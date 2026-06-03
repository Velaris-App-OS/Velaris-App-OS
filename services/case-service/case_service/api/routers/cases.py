"""Case instance API router.

Handles creation, retrieval, status changes, stage transitions,
and lifecycle operations for case instances.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.cases import (
    AssignmentResponse,
    AuditEntryResponse,
    CaseAction,
    CaseCreate,
    CaseDataUpdate,
    CaseListResponse,
    CasePriorityChange,
    CaseResolve,
    CaseResponse,
    CaseStageTransition,
    CaseStatusChange,
    RelationshipCreate,
    RelationshipResponse,
    StepCompleteBody,
    StepCompletionResponse,
)
from case_service.core import case_lifecycle
from case_service.core.outbound_rules import fire_outbound_rules
from case_service.db import repository as repo
from case_service.process_mining import event_logger
from case_service.realtime.publisher import publish_case_event, publish_assignment_event
from case_service.db.session import get_session
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.hxstream.emitter import emit_trace
from sqlalchemy import select as sa_select

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cases", tags=["cases"])


# ── SD-4: Role-gated PII field visibility ────────────────────────────────────

async def _get_pii_fields(session: AsyncSession, case_type_id: uuid.UUID) -> set[str]:
    """Return set of field names marked pii=True for a case type.

    FormFieldModel (the planned per-field PII registry) is not yet migrated.
    Until migration 042 adds that table, we return an empty set so callers
    get the permissive default (no redaction) rather than a 500.
    """
    return set()


def _has_sensitive_access(user: AuthenticatedUser) -> bool:
    """True if the user may see PII/sensitive case fields unredacted."""
    return user.is_admin or "finance" in user.roles or "admin" in user.roles


async def _redact_data(
    data: dict | None,
    case_type_id: uuid.UUID | None,
    user: AuthenticatedUser,
    session: AsyncSession,
) -> dict | None:
    """Return case data with PII fields replaced by '***' for non-privileged callers."""
    if data is None or not data:
        return data
    if _has_sensitive_access(user) or case_type_id is None:
        return data
    pii_fields = await _get_pii_fields(session, case_type_id)
    if not pii_fields:
        return data
    return {k: "***" if k in pii_fields else v for k, v in data.items()}


# ─── Schemas for new endpoints ────────────────────────────────────────


LOCK_TTL_SECONDS = 1800  # 30 minutes


class InternalAssignmentCreate(BaseModel):
    step_id: str
    assignee_type: str = "queue"
    assignee_id: str = "default-queue"


class MyTaskResponse(BaseModel):
    assignment_id: str
    case_id: str
    case_number: str | None
    case_description: str | None
    case_priority: str | None
    stage_id: str
    step_id: str
    step_def: dict
    form_id: str | None
    completion: dict | None
    locked_by: str | None
    lock_expires_at: str | None
    is_locked_by_me: bool


# ─── Helpers ──────────────────────────────────────────────────────────


async def _get_case_or_404(
    session: AsyncSession, case_id: uuid.UUID
):
    case = await repo.get_case_instance(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


async def _audit(
    session: AsyncSession,
    case_id: uuid.UUID,
    action: str,
    actor_id: str | None = None,
    **kwargs,
):
    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": action,
            "actor_id": actor_id,
            "actor_type": kwargs.pop("actor_type", "user"),
            "details": kwargs.pop("details", {}),
            "previous_value": kwargs.pop("previous_value", None),
            "new_value": kwargs.pop("new_value", None),
        },
    )


# ─── CRUD ─────────────────────────────────────────────────────────────


@router.post("", response_model=CaseResponse, status_code=201)
async def create_case(
    body: CaseCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a new case instance and start its lifecycle workflow."""
    # Verify case type exists
    case_type = await repo.get_case_type(session, body.case_type_id)
    if case_type is None:
        raise HTTPException(status_code=404, detail="Case type not found")

    # Verify the case type belongs to this user's tenant or is global.
    # Prevents a user in tenant A from creating cases of tenant B's case types.
    if case_type.tenant_id is not None and user.tenant_id:
        if str(case_type.tenant_id) != str(user.tenant_id):
            raise HTTPException(
                status_code=403,
                detail="This case type does not belong to your tenant.",
            )

    # Auto-derive tenant_id: use the user's tenant, fall back to "default"
    effective_tenant_id = str(user.tenant_id) if user.tenant_id else "default"

    case = await repo.create_case_instance(
        session,
        data={
            "case_type_id": body.case_type_id,
            "case_type_version": case_type.version,
            "status": "new",
            "priority": body.priority or case_type.default_priority,
            "data": body.data,
            "parent_case_id": body.parent_case_id,
            "created_by": body.created_by or user.user_id,
            "tenant_id": effective_tenant_id,
        },
    )

    await _audit(
        session,
        case.id,
        "created",
        actor_id=body.created_by,
        details={"case_type": case_type.name, "priority": case.priority},
    )

    # Start CaseLifecycleWorkflow via Temporal
    temporal_client = getattr(request.app.state, "temporal_client", None)
    if temporal_client is not None:
        try:
            from case_service.temporal.workflows.case_lifecycle_workflow import (
                CaseLifecycleWorkflow,
            )

            workflow_id = f"helix-case-{case.id}"
            await temporal_client.start_workflow(
                CaseLifecycleWorkflow.run,
                {
                    "case_id": str(case.id),
                    "case_type_id": str(case.case_type_id),
                },
                id=workflow_id,
                task_queue="helix-case-service",
            )

            await repo.update_case_instance(
                session,
                case.id,
                values={"process_instance_id": workflow_id},
            )

            logger.info(
                "Started lifecycle workflow %s for case %s",
                workflow_id, case.id,
            )
        except Exception as e:
            logger.warning(
                "Failed to start lifecycle workflow for case %s: %s",
                case.id, e,
            )
    else:
        logger.info(
            "Temporal not available — case %s created without "
            "auto-execution (manual lifecycle only)",
            case.id,
        )

    # Generate human-readable case number: HLX-{TYPE}-{NNNNNN}
    try:
        from sqlalchemy import text as _text
        seq_val = (await session.execute(_text("SELECT nextval('helix_case_seq')"))).scalar()
        prefix = re.sub(r"[^A-Za-z]", "", case_type.name).upper()[:3].ljust(3, "X")
        case_number = f"HLX-{prefix}-{seq_val:06d}"
        await repo.update_case_instance(session, case.id, values={"case_number": case_number})
    except Exception as e:
        logger.warning("Case number generation failed for %s: %s", case.id, e)

    # Process mining event log (Phase 14)
    try:
        await event_logger.log_case_created(
            session, case_id=case.id, case_type_id=case.case_type_id,
            actor_id=body.created_by,
        )
    except Exception as e:
        logger.warning("Event log failed for case %s: %s", case.id, e)

    # Fire outbound connector rules for case_created
    await fire_outbound_rules(
        session,
        trigger_event="case_created",
        case_id=case.id,
        case_type_id=case.case_type_id,
        case_data=body.data or {},
        tenant_id="default",
    )

    return await repo.get_case_instance(session, case.id)


@router.get("", response_model=CaseListResponse)
async def list_cases(
    status: str | None = None,
    priority: str | None = None,
    case_type_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Search / list case instances."""
    filters: dict = {}
    if status:
        filters["status"] = status
    if priority:
        filters["priority"] = priority
    if case_type_id:
        filters["case_type_id"] = case_type_id

    cases, total = await repo.search_case_instances(
        session,
        filters=filters,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return CaseListResponse(
        items=cases, total=total, page=page, page_size=page_size
    )


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    case = await _get_case_or_404(session, case_id)
    # SD-4: redact PII fields for non-privileged callers
    redacted = await _redact_data(case.data, case.case_type_id, user, session)
    if redacted is not case.data:
        response = CaseResponse.model_validate(case).model_dump()
        response["data"] = redacted
        return response
    return case


@router.patch("/{case_id}", response_model=CaseResponse)
async def update_case_data(
    case_id: uuid.UUID,
    body: CaseDataUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Merge-update the case's data payload."""
    case = await _get_case_or_404(session, case_id)
    merged = {**case.data, **body.data}
    await repo.update_case_instance(
        session, case_id, values={"data": merged}
    )
    await _audit(
        session,
        case_id,
        "data_updated",
        actor_id=body.updated_by,
        previous_value=case.data,
        new_value=merged,
    )

    # Phase 22: Real-time broadcast
    try:
        await publish_case_event(
            case_id, "data_updated",
            data={"updated_fields": list(body.data.keys())},
            actor_id=body.updated_by,
        )
    except Exception:
        pass

    return await repo.get_case_instance(session, case_id)


# ─── Status Changes ──────────────────────────────────────────────────


@router.post("/{case_id}/status", response_model=CaseResponse)
async def change_status(
    case_id: uuid.UUID,
    body: CaseStatusChange,
    session: AsyncSession = Depends(get_session),
):
    case = await _get_case_or_404(session, case_id)
    prev = case.status
    await repo.update_case_instance(
        session, case_id, values={"status": body.status}
    )
    await _audit(
        session,
        case_id,
        "status_changed",
        actor_id=body.actor_id,
        previous_value={"status": prev},
        new_value={"status": body.status},
        details={"reason": body.reason},
    )
    await case_lifecycle.on_status_changed(
        session, case_id=case_id, old_status=prev, new_status=body.status
    )

    # Phase 22: Real-time broadcast
    try:
        await publish_case_event(
            case_id, "status_changed",
            data={"old_status": prev, "new_status": body.status},
            actor_id=body.actor_id,
        )
    except Exception:
        pass

    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/priority", response_model=CaseResponse)
async def change_priority(
    case_id: uuid.UUID,
    body: CasePriorityChange,
    session: AsyncSession = Depends(get_session),
):
    case = await _get_case_or_404(session, case_id)
    prev = case.priority
    await repo.update_case_instance(
        session, case_id, values={"priority": body.priority}
    )
    await _audit(
        session,
        case_id,
        "priority_changed",
        actor_id=body.actor_id,
        previous_value={"priority": prev},
        new_value={"priority": body.priority},
    )
    await case_lifecycle.on_priority_changed(
        session, case_id=case_id, new_priority=body.priority
    )

    # Phase 22: Real-time broadcast
    try:
        await publish_case_event(
            case_id, "priority_changed",
            data={"old_priority": prev, "new_priority": body.priority},
            actor_id=body.actor_id,
        )
    except Exception:
        pass

    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/stage", response_model=CaseResponse)
async def transition_stage(
    case_id: uuid.UUID,
    body: CaseStageTransition,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    case = await _get_case_or_404(session, case_id)
    prev = case.current_stage_id
    await repo.update_case_instance(
        session, case_id, values={"current_stage_id": body.target_stage_id}
    )
    await _audit(
        session,
        case_id,
        "stage_transitioned",
        actor_id=user.user_id,
        previous_value={"stage_id": prev},
        new_value={"stage_id": body.target_stage_id},
    )
    await emit_trace(
        "stage_transition",
        {"from_stage": prev, "to_stage": body.target_stage_id},
        case_id=case_id,
        tenant_id="default",
        actor_user_id=user.user_id,
        session=session,
    )
    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/resolve", response_model=CaseResponse)
async def resolve_case(
    case_id: uuid.UUID,
    body: CaseResolve,
    session: AsyncSession = Depends(get_session),
):
    case = await _get_case_or_404(session, case_id)
    prev = case.status
    now = datetime.now(timezone.utc)
    await repo.update_case_instance(
        session,
        case_id,
        values={"status": "resolved", "resolved_at": now},
    )
    await _audit(
        session,
        case_id,
        "resolved",
        actor_id=body.actor_id,
        details={"resolution": body.resolution},
    )
    await case_lifecycle.on_status_changed(
        session, case_id=case_id, old_status=prev, new_status="resolved"
    )

    # Process mining event log (Phase 14)
    try:
        duration = None
        if case.created_at:
            from datetime import timezone as _tz
            created = case.created_at if case.created_at.tzinfo else case.created_at.replace(tzinfo=_tz.utc)
            duration = int((now - created).total_seconds())
        await event_logger.log_case_resolved(
            session, case_id=case_id, case_type_id=case.case_type_id,
            actor_id=body.actor_id, duration_seconds=duration,
        )
    except Exception as e:
        logger.warning("Event log failed for case %s: %s", case_id, e)

    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/close", response_model=CaseResponse)
async def close_case(
    case_id: uuid.UUID,
    body: CaseAction,
    session: AsyncSession = Depends(get_session),
):
    case = await _get_case_or_404(session, case_id)
    prev = case.status
    now = datetime.now(timezone.utc)
    await repo.update_case_instance(
        session,
        case_id,
        values={"status": "closed", "closed_at": now},
    )
    await _audit(session, case_id, "closed", actor_id=body.actor_id)
    await case_lifecycle.on_status_changed(
        session, case_id=case_id, old_status=prev, new_status="closed"
    )
    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/reopen", response_model=CaseResponse)
async def reopen_case(
    case_id: uuid.UUID,
    body: CaseAction,
    session: AsyncSession = Depends(get_session),
):
    case = await _get_case_or_404(session, case_id)
    prev = case.status
    await repo.update_case_instance(
        session, case_id, values={"status": "reopened"}
    )
    await _audit(
        session,
        case_id,
        "reopened",
        actor_id=body.actor_id,
        details={"reason": body.reason},
    )
    await case_lifecycle.on_status_changed(
        session, case_id=case_id, old_status=prev, new_status="reopened"
    )
    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/cancel", response_model=CaseResponse)
async def cancel_case(
    case_id: uuid.UUID,
    body: CaseAction,
    session: AsyncSession = Depends(get_session),
):
    case = await _get_case_or_404(session, case_id)
    prev = case.status
    await repo.update_case_instance(
        session, case_id, values={"status": "cancelled"}
    )
    await _audit(
        session,
        case_id,
        "cancelled",
        actor_id=body.actor_id,
        details={"reason": body.reason},
    )
    await case_lifecycle.on_status_changed(
        session, case_id=case_id, old_status=prev, new_status="cancelled"
    )
    return await repo.get_case_instance(session, case_id)


# ─── Assignments (for workflow activities) ────────────────────────────


@router.post(
    "/{case_id}/assignments",
    response_model=AssignmentResponse,
    status_code=201,
)
async def create_assignment(
    case_id: uuid.UUID,
    body: InternalAssignmentCreate,
    session: AsyncSession = Depends(get_session),
):
    """Create a work-item assignment for a case step.

    Called by Temporal activities during stage execution.
    """
    await _get_case_or_404(session, case_id)

    assignment = await repo.create_assignment(
        session,
        data={
            "case_id": case_id,
            "step_id": body.step_id,
            "assignee_type": body.assignee_type,
            "assignee_id": body.assignee_id,
        },
    )

    await _audit(
        session,
        case_id,
        "assignment_created",
        actor_type="system",
        details={
            "step_id": body.step_id,
            "assignee_type": body.assignee_type,
            "assignee_id": body.assignee_id,
        },
    )

    return assignment


@router.get(
    "/{case_id}/assignments",
    response_model=list[AssignmentResponse],
)
async def list_case_assignments(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """List all assignments for a case."""
    await _get_case_or_404(session, case_id)
    return await repo.get_assignments_for_case(session, case_id)


# ─── History ──────────────────────────────────────────────────────────


@router.get(
    "/{case_id}/history", response_model=list[AuditEntryResponse]
)
async def get_case_history(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    await _get_case_or_404(session, case_id)
    return await repo.get_audit_log(session, case_id)


# ─── Relationships ────────────────────────────────────────────────────


@router.post(
    "/{case_id}/relationships",
    response_model=RelationshipResponse,
    status_code=201,
)
async def add_relationship(
    case_id: uuid.UUID,
    body: RelationshipCreate,
    session: AsyncSession = Depends(get_session),
):
    await _get_case_or_404(session, case_id)
    await _get_case_or_404(session, body.target_case_id)
    rel = await repo.create_relationship(
        session,
        data={
            "source_case_id": case_id,
            "target_case_id": body.target_case_id,
            "relationship_type": body.relationship_type,
            "propagate_status": body.propagate_status,
            "propagate_priority": body.propagate_priority,
            "required": body.required,
        },
    )
    await _audit(
        session,
        case_id,
        "relationship_added",
        details={
            "target_case_id": str(body.target_case_id),
            "type": body.relationship_type,
        },
    )
    return rel


@router.get(
    "/{case_id}/relationships",
    response_model=list[RelationshipResponse],
)
async def list_relationships(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    await _get_case_or_404(session, case_id)
    return await repo.get_relationships(session, case_id)


# ─── Child Cases ──────────────────────────────────────────────────────


@router.post(
    "/{case_id}/children", response_model=CaseResponse, status_code=201
)
async def create_child_case(
    case_id: uuid.UUID,
    body: CaseCreate,
    session: AsyncSession = Depends(get_session),
):
    """Create a child case linked to the parent."""
    parent = await _get_case_or_404(session, case_id)
    body.parent_case_id = case_id
    case_type = await repo.get_case_type(session, body.case_type_id)
    if case_type is None:
        raise HTTPException(status_code=404, detail="Case type not found")

    child = await repo.create_case_instance(
        session,
        data={
            "case_type_id": body.case_type_id,
            "case_type_version": case_type.version,
            "status": "new",
            "priority": body.priority or case_type.default_priority,
            "data": body.data,
            "parent_case_id": case_id,
            "created_by": body.created_by,
        },
    )

    await repo.create_relationship(
        session,
        data={
            "source_case_id": case_id,
            "target_case_id": child.id,
            "relationship_type": "child",
            "propagate_status": True,
        },
    )

    await _audit(
        session,
        case_id,
        "child_case_created",
        actor_id=body.created_by,
        details={"child_case_id": str(child.id)},
    )

    return child


@router.get(
    "/{case_id}/children", response_model=list[CaseResponse]
)
async def list_child_cases(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    await _get_case_or_404(session, case_id)
    children, _ = await repo.search_case_instances(
        session,
        filters={"parent_case_id": case_id},
        limit=200,
    )
    return children


# ── P38: Step Completions ─────────────────────────────────────────────────────

async def _auto_advance_if_complete(
    session: AsyncSession,
    case_id: uuid.UUID,
    stage_id: str,
    case_type_def: dict,
) -> str | None:
    """Check if all required steps in stage_id are complete; if so advance to next stage.

    Returns the new stage_id if advanced, else None.
    """
    from sqlalchemy import select
    from case_service.db.models import CaseStepCompletionModel

    stages = sorted(
        case_type_def.get("stages", []),
        key=lambda s: s.get("order", 0),
    )
    current_stage = next((s for s in stages if s["id"] == stage_id), None)
    if current_stage is None:
        return None

    required_step_ids = {
        s["id"] for s in current_stage.get("steps", [])
        if s.get("required", True) and s.get("step_type") != "automated"
    }
    if not required_step_ids:
        return None  # no required steps → don't auto-advance (avoid infinite loops)

    completed = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case_id,
            CaseStepCompletionModel.stage_id == stage_id,
            CaseStepCompletionModel.status == "completed",
        )
    )).scalars().all()

    completed_ids = {c.step_id for c in completed}
    if not required_step_ids.issubset(completed_ids):
        return None  # not all required steps done yet

    # Find the next stage
    current_idx = next((i for i, s in enumerate(stages) if s["id"] == stage_id), -1)
    if current_idx < 0 or current_idx >= len(stages) - 1:
        return None  # already at last stage

    next_stage_id = stages[current_idx + 1]["id"]
    await repo.update_case_instance(session, case_id, values={"current_stage_id": next_stage_id})
    await _audit(
        session, case_id, "stage_auto_advanced",
        previous_value={"stage_id": stage_id},
        new_value={"stage_id": next_stage_id},
    )

    # Fetch case for outbound rule context
    case_row = await repo.get_case_instance(session, case_id)
    if case_row:
        await fire_outbound_rules(
            session,
            trigger_event="stage_exit",
            case_id=case_id,
            case_type_id=case_row.case_type_id,
            case_data=case_row.data or {},
            tenant_id="default",
        )
        await fire_outbound_rules(
            session,
            trigger_event="stage_enter",
            case_id=case_id,
            case_type_id=case_row.case_type_id,
            case_data=case_row.data or {},
            tenant_id="default",
        )

    return next_stage_id


@router.post("/{case_id}/steps/{step_id}/complete", response_model=StepCompletionResponse)
async def complete_step(
    case_id: uuid.UUID,
    step_id: str,
    body: StepCompleteBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Record a step completion (or rejection). Upserts on re-submit.

    After recording, checks whether all required steps in the stage are complete
    and auto-advances the case to the next stage if so.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy import select
    from case_service.db.models import CaseStepCompletionModel, CaseTypeModel

    case = await _get_case_or_404(session, case_id)

    # Load case type definition for auto-advance check
    ct = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id)
    )).scalar_one_or_none()
    ct_def = ct.definition_json if ct else {}

    # Validate lock ownership — if a live lock exists it must belong to this operator
    from case_service.db.models import CaseAssignmentModel
    now = datetime.now(timezone.utc)
    active_assignment = (await session.execute(
        select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id,
            CaseAssignmentModel.step_id == step_id,
            CaseAssignmentModel.status == "active",
        ).order_by(CaseAssignmentModel.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()

    if active_assignment is not None and active_assignment.locked_by is not None:
        exp = active_assignment.lock_expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        lock_valid = exp is not None and exp > now
        if lock_valid and active_assignment.locked_by != user.user_id:
            raise HTTPException(
                status_code=409,
                detail=f"Step is locked by operator '{active_assignment.locked_by}'. Try again after the lock expires.",
            )

    # Upsert step completion
    existing = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case_id,
            CaseStepCompletionModel.step_id == step_id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.stage_id = body.stage_id
        existing.step_type = body.step_type
        existing.status = body.status
        existing.data = body.data
        existing.completed_by = body.actor_id or user.user_id
        existing.completed_at = now
        completion = existing
    else:
        completion = CaseStepCompletionModel(
            case_id=case_id,
            stage_id=body.stage_id,
            step_id=step_id,
            step_type=body.step_type,
            status=body.status,
            data=body.data,
            completed_by=body.actor_id or user.user_id,
            completed_at=now,
        )
        session.add(completion)

    # Close assignment and release lock after successful submission
    if active_assignment is not None:
        active_assignment.status = "completed"
        active_assignment.completed_at = now
        active_assignment.locked_by = None
        active_assignment.locked_at = None
        active_assignment.lock_expires_at = None

    await session.flush()

    await _audit(
        session, case_id, "step_completed",
        actor_id=body.actor_id or user.user_id,
        details={"step_id": step_id, "stage_id": body.stage_id, "status": body.status},
    )
    await emit_trace(
        "step_complete",
        {"step_id": step_id, "stage_id": body.stage_id, "status": body.status,
         "step_type": body.step_type},
        case_id=case_id,
        tenant_id="default",
        actor_user_id=user.user_id,
        session=session,
    )

    # Fire outbound connector rules for step_complete
    if body.status == "completed":
        await fire_outbound_rules(
            session,
            trigger_event="step_complete",
            case_id=case_id,
            case_type_id=case.case_type_id,
            case_data=case.data or {},
            tenant_id="default",
        )

    # Auto-advance if all required steps are done
    auto_advanced = False
    if body.status == "completed" and ["open", "new", "reopened"].__contains__(case.status):
        new_stage = await _auto_advance_if_complete(
            session, case_id, body.stage_id, ct_def
        )
        auto_advanced = new_stage is not None

    return StepCompletionResponse(
        id=str(completion.id),
        case_id=str(completion.case_id),
        stage_id=completion.stage_id,
        step_id=completion.step_id,
        step_type=completion.step_type,
        status=completion.status,
        data=completion.data,
        completed_by=completion.completed_by,
        completed_at=completion.completed_at.isoformat(),
        auto_advanced=auto_advanced,
    )


@router.post("/{case_id}/steps/{step_id}/activate")
async def activate_step(
    case_id: uuid.UUID,
    step_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Activate a step — behaviour depends on step_type defined in the case type.

    user_task   → no-op (worker picks it up manually, existing flow)
    service_task → auto-execute linked connector, then auto-complete the step
    subprocess  → start linked HxFusion process definition, bind to this case+step
    approval    → create an approval request record (future: notify approver)
    script_task → evaluate inline rule expression, auto-complete
    """
    from sqlalchemy import select
    from case_service.db.models import CaseTypeModel, CaseStepCompletionModel

    case = await _get_case_or_404(session, case_id)
    ct = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id)
    )).scalar_one_or_none()
    if not ct:
        raise HTTPException(404, "Case type not found")

    # Find step definition in case type JSON
    ct_def = ct.definition_json or {}
    step_def: dict | None = None
    found_stage_id: str = ""
    for stage in ct_def.get("stages", []):
        for step in stage.get("steps", []):
            if step.get("id") == step_id:
                step_def = step
                found_stage_id = stage.get("id", "")
                break
        if step_def:
            break

    if not step_def:
        raise HTTPException(404, f"Step {step_id} not found in case type definition")

    step_type = step_def.get("step_type", "user_task")
    now = datetime.now(timezone.utc)
    result: dict = {"step_id": step_id, "step_type": step_type, "action": "none"}

    if step_type in ("service_task", "connector_call", "send_task"):
        # All connector-backed automated steps use connector_config
        connector_config = step_def.get("connector_config", {})
        connector_id = connector_config.get("connector_id")
        if not connector_id:
            raise HTTPException(400, "service_task has no connector_id configured")

        # Execute connector via HxBridge
        connector_result: dict = {}
        try:
            from case_service.hxbridge.protocol import get_connector
            connector = await get_connector(connector_id, session)
            if connector is None:
                raise ValueError(f"Connector '{connector_id}' not found")

            # Build input from case data using input_mapping
            input_mapping: dict = connector_config.get("input_mapping", {})
            case_data = case.data or {}
            payload = {k: case_data.get(v, v) for k, v in input_mapping.items()} if input_mapping else dict(case_data)
            connector_result = await connector.execute(payload) or {}
        except Exception as e:
            connector_result = {"_error": str(e)}

        # Write result back to case data using output_mapping
        output_mapping: dict = connector_config.get("output_mapping", {})
        if output_mapping and isinstance(connector_result, dict):
            new_data = dict(case.data or {})
            for src, dest in output_mapping.items():
                if src in connector_result:
                    new_data[dest] = connector_result[src]
            case.data = new_data

        # Auto-complete the step
        completion = CaseStepCompletionModel(
            case_id=case_id,
            stage_id=found_stage_id,
            step_id=step_id,
            step_type=step_type,
            status="completed",
            data={"connector_result": connector_result},
            completed_by="system",
            completed_at=now,
        )
        session.add(completion)
        result = {"step_id": step_id, "step_type": step_type, "action": "executed", "connector_result": connector_result}

    elif step_type == "subprocess":
        # process_definition_id lives inside subprocess_config (as stored by PropertyPanel)
        subprocess_config = step_def.get("subprocess_config", {})
        process_def_id = subprocess_config.get("process_definition_id") or step_def.get("process_definition_id")
        if not process_def_id:
            raise HTTPException(400, "subprocess step has no process_definition_id configured")
        try:
            from case_service.hxfusion.engine import start_instance as _start_fusion
            from case_service.db.models import ProcessDefinitionModel
            defn = await session.get(ProcessDefinitionModel, uuid.UUID(process_def_id))
            if not defn or defn.status != "active":
                raise HTTPException(400, f"Process definition {process_def_id} not found or inactive")

            # Map case fields to process context using context_mapping
            context_mapping: dict = subprocess_config.get("context_mapping", {})
            case_data = case.data or {}
            context = dict(case_data)
            if context_mapping:
                try:
                    import json as _json
                    if isinstance(context_mapping, str):
                        context_mapping = _json.loads(context_mapping)
                    context = {k: case_data.get(v, v) for k, v in context_mapping.items()}
                except Exception:
                    pass

            proc = await _start_fusion(
                definition_id=defn.id,
                case_id=case_id,
                context={**context, "step_id": step_id},
                tenant_id=str(ct.tenant_id) if ct.tenant_id else None,
                stage_id=found_stage_id,
                step_id=step_id,
                session=session,
            )
            result = {"step_id": step_id, "step_type": "subprocess", "action": "process_started", "process_instance_id": str(proc.id)}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Failed to start subprocess: {e}")

    elif step_type in ("user_task", "manual_task", "approval"):
        result = {"step_id": step_id, "step_type": step_type, "action": "awaiting_human"}

    else:
        result = {"step_id": step_id, "step_type": step_type, "action": "no_op"}

    await session.commit()
    return result


@router.get("/{case_id}/step-completions", response_model=list[StepCompletionResponse])
async def list_step_completions(
    case_id: uuid.UUID,
    stage_id: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """Return all step completions for a case, optionally filtered by stage."""
    from sqlalchemy import select
    from case_service.db.models import CaseStepCompletionModel

    await _get_case_or_404(session, case_id)
    stmt = select(CaseStepCompletionModel).where(
        CaseStepCompletionModel.case_id == case_id
    )
    if stage_id:
        stmt = stmt.where(CaseStepCompletionModel.stage_id == stage_id)
    rows = (await session.execute(stmt.order_by(CaseStepCompletionModel.completed_at))).scalars().all()
    return [
        StepCompletionResponse(
            id=str(r.id), case_id=str(r.case_id),
            stage_id=r.stage_id, step_id=r.step_id,
            step_type=r.step_type, status=r.status,
            data=r.data, completed_by=r.completed_by,
            completed_at=r.completed_at.isoformat(),
        )
        for r in rows
    ]


@router.get("/{case_id}/my-task", response_model=MyTaskResponse | None)
async def get_my_task(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return the operator's current open assignment on this case, acquiring a lock.

    Pega semantics: opening a task locks it to this operator for LOCK_TTL_SECONDS.
    Other operators can see the lock but cannot submit the step while it is held.
    Returns null (204 body) when no active assignment exists for this operator.
    """
    from sqlalchemy import select
    from case_service.db.models import (
        CaseAssignmentModel, CaseTypeModel, CaseStepCompletionModel
    )

    case = await _get_case_or_404(session, case_id)
    now = datetime.now(timezone.utc)

    # Find most recent active assignment for this operator
    assignment = (await session.execute(
        select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id,
            CaseAssignmentModel.assignee_id == user.user_id,
            CaseAssignmentModel.status == "active",
        ).order_by(CaseAssignmentModel.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()

    if assignment is None:
        return None

    # Acquire / refresh lock
    lock_expires = now + timedelta(seconds=LOCK_TTL_SECONDS)
    assignment.locked_by = user.user_id
    assignment.locked_at = now
    assignment.lock_expires_at = lock_expires
    await session.flush()
    await emit_trace(
        "lock_acquire",
        {"step_id": assignment.step_id, "lock_expires_at": lock_expires.isoformat()},
        case_id=case_id,
        tenant_id="default",
        actor_user_id=user.user_id,
        session=session,
    )

    # Resolve step definition from case type
    ct = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id)
    )).scalar_one_or_none()
    ct_def = ct.definition_json if ct else {}

    stage_id = case.current_stage_id or ""
    step_def: dict = {}
    form_id: str | None = None
    for stage in ct_def.get("stages", []):
        for step in stage.get("steps", []):
            if step.get("id") == assignment.step_id:
                step_def = step
                form_id = step.get("form_id")
                if not stage_id:
                    stage_id = stage.get("id", "")
                break

    # Load existing completion if any
    completion_row = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case_id,
            CaseStepCompletionModel.step_id == assignment.step_id,
        )
    )).scalar_one_or_none()

    return MyTaskResponse(
        assignment_id=str(assignment.id),
        case_id=str(case.id),
        case_number=case.case_number,
        case_description=case.data.get("description") if case.data else None,
        case_priority=case.priority,
        stage_id=stage_id,
        step_id=assignment.step_id,
        step_def=step_def,
        form_id=form_id,
        completion=completion_row.data if completion_row else None,
        locked_by=assignment.locked_by,
        lock_expires_at=lock_expires.isoformat(),
        is_locked_by_me=True,
    )


@router.post("/{case_id}/steps/{step_id}/unlock", status_code=204)
async def unlock_step(
    case_id: uuid.UUID,
    step_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Explicitly release a step lock held by the calling operator.

    Called when the operator navigates away or clicks Cancel — prevents
    the lock from blocking colleagues for the full TTL.
    """
    from sqlalchemy import select
    from case_service.db.models import CaseAssignmentModel

    assignment = (await session.execute(
        select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id,
            CaseAssignmentModel.step_id == step_id,
            CaseAssignmentModel.status == "active",
            CaseAssignmentModel.locked_by == user.user_id,
        ).limit(1)
    )).scalar_one_or_none()

    if assignment is not None:
        assignment.locked_by = None
        assignment.locked_at = None
        assignment.lock_expires_at = None
        await session.flush()
        await emit_trace(
            "lock_release",
            {"step_id": step_id},
            case_id=case_id,
            tenant_id="default",
            actor_user_id=user.user_id,
            session=session,
        )
