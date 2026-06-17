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
from pydantic import BaseModel, Field
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
from case_service.integrations.webhook_dispatcher import (
    dispatch_event as _dispatch_webhook,
    build_case_event_payload as _webhook_payload,
)
from case_service.process_mining import event_logger
from case_service.realtime.publisher import publish_case_event, publish_assignment_event
from case_service.db.session import get_session
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.hxstream.emitter import emit_trace
from case_service.case_vars import service as case_vars
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


async def _signal_lifecycle(
    request: Request,
    case_id: uuid.UUID,
    signal: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort nudge to the case's SLA companion workflow.

    The companion rescans periodically regardless, so a failed or skipped
    signal only delays a timer — routes never fail because of Temporal.
    """
    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        return
    try:
        handle = client.get_workflow_handle(f"helix-case-{case_id}")
        await handle.signal(signal, payload or {})
    except Exception as e:
        logger.debug(
            "Lifecycle signal %s skipped for case %s: %s", signal, case_id, e
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

    # Derive the case's tenant (case_instances.tenant_id is a nullable UUID):
    #   1. the acting user's tenant, when they have one;
    #   2. else inherit the case type's tenant — covers user-less creation
    #      (e.g. HxFusion: an external system opens a case after a process);
    #   3. else None — global / single-tenant cases are stored with NULL tenant.
    # Must never be a non-UUID string ("default" used to be inserted here and
    # crashed the UUID column for tenant-less users).
    if user.tenant_id:
        effective_tenant_id = str(user.tenant_id)
    elif case_type.tenant_id is not None:
        effective_tenant_id = str(case_type.tenant_id)
    else:
        effective_tenant_id = None

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

    # Start case-level SLAs + initial urgency (synchronous path owns
    # SLA state; the Temporal companion only adds durable timers).
    try:
        await case_lifecycle.on_case_created(
            session,
            case_id=case.id,
            case_type_def=case_type.definition_json or {},
            case_type_id=case.case_type_id,
            tenant_id=effective_tenant_id,
        )
    except Exception as e:
        logger.warning("SLA start on case creation failed for %s: %s", case.id, e)

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

    # Queue webhook delivery via transactional outbox
    try:
        wh_payload = await _webhook_payload(session, case.id)
        await _dispatch_webhook(session, "case.created", wh_payload, case.case_type_id)
    except Exception as _e:
        logger.warning("outbox dispatch failed for case %s: %s", case.id, _e)

    return await repo.get_case_instance(session, case.id)


@router.get("", response_model=CaseListResponse)
async def list_cases(
    status: str | None = None,
    priority: str | None = None,
    case_type_id: uuid.UUID | None = None,
    var: list[str] | None = Query(None, description=
        "Variable filter 'namespace.name:value' — repeatable, AND semantics. "
        "Only variables flagged indexed in the Case Designer are filterable."),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Search / list case instances. Auth added with HxGuard Phase B — this
    route previously returned every case (data blobs included) without a
    token. Per-user relationship FILTERING of the list is a Phase B
    follow-up (enforce-mode cutover)."""
    filters: dict = {}
    if status:
        filters["status"] = status
    if priority:
        filters["priority"] = priority
    if case_type_id:
        filters["case_type_id"] = case_type_id

    # Case Variables Phase 3: indexed variable filters
    variable_filters: list[tuple[str, str]] = []
    if var:
        from case_service.db.models import CaseTypeVariableModel, VariableNamespaceModel
        from case_service.case_vars.service import NAME_RE, _effective_sensitivity
        for raw_filter in var[:10]:                      # sane cap
            full_key, sep, value = raw_filter.partition(":")
            ns_name, dot, var_name = full_key.partition(".")
            if not sep or not dot or not NAME_RE.match(ns_name) or not NAME_RE.match(var_name):
                raise HTTPException(400, f"var filter must be 'namespace.name:value', got {raw_filter!r}")
            idx_q = sa_select(CaseTypeVariableModel).where(
                CaseTypeVariableModel.full_key == full_key,
                CaseTypeVariableModel.indexed == True,  # noqa: E712
            )
            if case_type_id:
                idx_q = idx_q.where(CaseTypeVariableModel.case_type_id == case_type_id)
            indexed_def = (await session.execute(idx_q.limit(1))).scalar_one_or_none()
            if indexed_def is None:
                raise HTTPException(400, f"'{full_key}' is not an indexed variable — flag it in the Case Designer Variables panel first")
            # Equality filtering is a value oracle: it confirms a guessed
            # value even though reads come back masked. pii/secret variables
            # are therefore never filterable, indexed or not.
            ns_row = (await session.execute(
                sa_select(VariableNamespaceModel)
                .where(VariableNamespaceModel.name == ns_name).limit(1)
            )).scalar_one_or_none()
            if ns_row is not None and _effective_sensitivity(ns_row, indexed_def) in ("pii", "secret"):
                raise HTTPException(400, f"'{full_key}' is {_effective_sensitivity(ns_row, indexed_def)}-classified — sensitive variables are not filterable")
            variable_filters.append((full_key, value))

    # HxGuard enforce cutover: in enforce mode the list shows only cases the
    # user has a relationship with (owner / tuple / allowed case type).
    # Admin, manager, and "*"-scoped access groups see everything.
    accessible_to_user = None
    from case_service.config import get_settings
    if (get_settings().hxguard_case_enforcement or "shadow").lower() == "enforce" \
            and not (user.is_admin or "superadmin" in (user.roles or [])
                     or "manager" in (user.roles or [])):
        from case_service.db.models import AccessGroupModel, OperatorAccessGroupModel
        allowed_lists = (await session.execute(
            sa_select(AccessGroupModel.allowed_case_type_ids)
            .join(OperatorAccessGroupModel,
                  OperatorAccessGroupModel.access_group_id == AccessGroupModel.id)
            .where(OperatorAccessGroupModel.operator_id == user.user_id)
            .where(AccessGroupModel.is_active == True)  # noqa: E712
        )).scalars().all()
        allowed_ids: list = []
        unrestricted = False
        for lst in allowed_lists:
            if "*" in (lst or []):
                unrestricted = True
                break
            allowed_ids.extend(uuid.UUID(x) for x in (lst or []) if x)
        if not unrestricted:
            accessible_to_user = (user.user_id, allowed_ids)

    cases, total = await repo.search_case_instances(
        session,
        filters=filters,
        offset=(page - 1) * page_size,
        limit=page_size,
        variable_filters=variable_filters or None,
        accessible_to_user=accessible_to_user,
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
    # HxGuard Phase B pilot (shadow by default — see hxguard_case_enforcement)
    from case_service import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)
    # case_vars façade read: typed variables (sensitivity-redacted in the
    # service) merged over the case.data blob. SD-4 redaction maps still
    # apply to the blob keys afterwards.
    ctx = case_vars.CallerContext(
        kind="platform", actor_id=user.user_id,
        privileged=_has_sensitive_access(user),
    )
    data = await case_vars.get_all(session, ctx, case_id)
    redacted = await _redact_data(data, case.case_type_id, user, session)
    response = CaseResponse.model_validate(case).model_dump()
    response["data"] = redacted
    return response


@router.patch("/{case_id}", response_model=CaseResponse)
async def update_case_data(
    case_id: uuid.UUID,
    body: CaseDataUpdate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Merge-update the case's data payload."""
    case = await _get_case_or_404(session, case_id)
    # HxGuard Phase B pilot — also closes the pre-existing unauthenticated-
    # mutation gap (route previously had NO auth dependency)
    from case_service import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
    # Phase 4 guard: a promoted key's blob value is no longer served by the
    # façade — writing it here would vanish silently. Reject with a pointer.
    if body.data:
        from case_service.db.models import CaseTypeVariableModel
        promoted = (await session.execute(
            sa_select(CaseTypeVariableModel.promoted_source, CaseTypeVariableModel.full_key)
            .where(CaseTypeVariableModel.case_type_id == case.case_type_id)
            .where(CaseTypeVariableModel.promoted_source.in_(list(body.data.keys())))
        )).all()
        if promoted:
            detail = ", ".join(f"'{s}' is promoted to {fk}" for s, fk in promoted)
            raise HTTPException(400, f"{detail} — these keys are typed variables now; write them through the owning integration or the variables API")
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

    # Queue webhook delivery via transactional outbox — variable NAMES only,
    # values never leave the API boundary in a webhook
    try:
        wh_payload = await _webhook_payload(session, case_id,
            extra={"changed_variables": sorted(body.data.keys())})
        await _dispatch_webhook(session, "case.data_updated", wh_payload,
            case.case_type_id)
    except Exception as _e:
        logger.warning("outbox dispatch failed for data update %s: %s", case_id, _e)

    return await repo.get_case_instance(session, case_id)


# ─── Status Changes ──────────────────────────────────────────────────


async def apply_status_change(
    session: AsyncSession,
    case_id: uuid.UUID,
    new_status: str,
    actor_id: str | None,
    reason: str | None = None,
) -> None:
    """The FULL status-change machinery — audit, SLA lifecycle, realtime,
    outbox webhook. Used by the status endpoint AND rule-action application
    (velaris.status writes route here; state never changes silently)."""
    case = await _get_case_or_404(session, case_id)
    prev = case.status
    await repo.update_case_instance(
        session, case_id, values={"status": new_status}
    )
    await _audit(
        session,
        case_id,
        "status_changed",
        actor_id=actor_id,
        previous_value={"status": prev},
        new_value={"status": new_status},
        details={"reason": reason},
    )
    await case_lifecycle.on_status_changed(
        session, case_id=case_id, old_status=prev, new_status=new_status
    )

    # Phase 22: Real-time broadcast
    try:
        await publish_case_event(
            case_id, "status_changed",
            data={"old_status": prev, "new_status": new_status},
            actor_id=actor_id,
        )
    except Exception:
        pass

    # Queue webhook delivery via transactional outbox
    try:
        case_obj = await repo.get_case_instance(session, case_id)
        wh_payload = await _webhook_payload(session, case_id,
            extra={"old_status": prev, "new_status": new_status})
        await _dispatch_webhook(session, "case.status_changed", wh_payload,
            case_obj.case_type_id if case_obj else None)
    except Exception as _e:
        logger.warning("outbox dispatch failed for status change %s: %s", case_id, _e)


@router.post("/{case_id}/status", response_model=CaseResponse)
async def change_status(
    case_id: uuid.UUID,
    body: CaseStatusChange,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
    await apply_status_change(session, case_id, body.status, body.actor_id, body.reason)
    await _signal_lifecycle(request, case_id, "status_changed", {"status": body.status})
    return await repo.get_case_instance(session, case_id)


async def apply_priority_change(
    session: AsyncSession,
    case_id: uuid.UUID,
    new_priority: str,
    actor_id: str | None,
) -> None:
    """Full priority-change machinery — shared by the endpoint and
    rule-action application (velaris.priority writes route here)."""
    case = await _get_case_or_404(session, case_id)
    prev = case.priority
    await repo.update_case_instance(
        session, case_id, values={"priority": new_priority}
    )
    await _audit(
        session,
        case_id,
        "priority_changed",
        actor_id=actor_id,
        previous_value={"priority": prev},
        new_value={"priority": new_priority},
    )
    await case_lifecycle.on_priority_changed(
        session, case_id=case_id, new_priority=new_priority
    )

    # Phase 22: Real-time broadcast
    try:
        await publish_case_event(
            case_id, "priority_changed",
            data={"old_priority": prev, "new_priority": new_priority},
            actor_id=actor_id,
        )
    except Exception:
        pass

    # Queue webhook delivery via transactional outbox
    try:
        case_obj = await repo.get_case_instance(session, case_id)
        wh_payload = await _webhook_payload(session, case_id,
            extra={"old_priority": prev, "new_priority": new_priority})
        await _dispatch_webhook(session, "case.priority_changed", wh_payload,
            case_obj.case_type_id if case_obj else None)
    except Exception as _e:
        logger.warning("outbox dispatch failed for priority change %s: %s", case_id, _e)


@router.post("/{case_id}/priority", response_model=CaseResponse)
async def change_priority(
    case_id: uuid.UUID,
    body: CasePriorityChange,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
    await apply_priority_change(session, case_id, body.priority, body.actor_id)
    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/stage", response_model=CaseResponse)
async def transition_stage(
    case_id: uuid.UUID,
    body: CaseStageTransition,
    request: Request,
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

    # Start the new stage's SLA (idempotent) and nudge the SLA companion
    try:
        ct = await repo.get_case_type(session, case.case_type_id)
        ct_def = (ct.definition_json or {}) if ct else {}
        stage = next(
            (s for s in ct_def.get("stages", []) if s["id"] == body.target_stage_id),
            None,
        )
        if stage is not None:
            await case_lifecycle.start_stage_slas(
                session,
                case_id=case_id,
                case_type_id=case.case_type_id,
                stage=stage,
                case_type_def=ct_def,
                tenant_id=getattr(case, "tenant_id", None),
            )
    except Exception as e:
        logger.warning("Stage SLA start failed for case %s: %s", case_id, e)
    await _signal_lifecycle(
        request, case_id, "stage_entered", {"stage_id": body.target_stage_id}
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
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
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

    await _signal_lifecycle(request, case_id, "status_changed", {"status": "resolved"})
    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/close", response_model=CaseResponse)
async def close_case(
    case_id: uuid.UUID,
    body: CaseAction,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
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
    await _signal_lifecycle(request, case_id, "status_changed", {"status": "closed"})
    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/reopen", response_model=CaseResponse)
async def reopen_case(
    case_id: uuid.UUID,
    body: CaseAction,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
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
    # The companion workflow exits when a case reaches a terminal status,
    # so reopening must restart it before signalling.
    client = getattr(request.app.state, "temporal_client", None)
    if client is not None:
        try:
            from case_service.temporal.workflows.case_lifecycle_workflow import (
                CaseLifecycleWorkflow,
            )
            await client.start_workflow(
                CaseLifecycleWorkflow.run,
                {"case_id": str(case_id), "case_type_id": str(case.case_type_id)},
                id=f"helix-case-{case_id}",
                task_queue="helix-case-service",
            )
        except Exception:
            pass  # already running — the signal below is enough
    await _signal_lifecycle(request, case_id, "status_changed", {"status": "reopened"})
    return await repo.get_case_instance(session, case_id)


@router.post("/{case_id}/cancel", response_model=CaseResponse)
async def cancel_case(
    case_id: uuid.UUID,
    body: CaseAction,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
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
    await _signal_lifecycle(request, case_id, "status_changed", {"status": "cancelled"})
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

    # Start the new stage's SLA here so every auto-advance caller
    # (step completion, payments, kyc, crm, ...) gets it.
    try:
        await case_lifecycle.start_stage_slas(
            session,
            case_id=case_id,
            case_type_id=None,
            stage=stages[current_idx + 1],
            case_type_def=case_type_def,
        )
    except Exception as e:
        logger.warning("Stage SLA start failed on auto-advance %s: %s", case_id, e)

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
    request: Request,
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
            case_data=await case_vars.get_all(
                session,
                case_vars.CallerContext(kind="rules", actor_id="outbound-rules"),
                case_id,
            ),
            tenant_id="default",
        )

    # Auto-advance if all required steps are done
    auto_advanced = False
    if body.status == "completed" and ["open", "new", "reopened"].__contains__(case.status):
        new_stage = await _auto_advance_if_complete(
            session, case_id, body.stage_id, ct_def
        )
        auto_advanced = new_stage is not None

    # Nudge the SLA companion (new stage may have started an SLA)
    await _signal_lifecycle(
        request, case_id,
        "stage_entered" if auto_advanced else "step_completed",
        {"step_id": step_id},
    )

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
        # The connector reads and writes case variables AS itself: it sees
        # its own secret variables unmasked, everyone else's pii/secret
        # redacted, and its writes land in its registered namespace.
        try:
            _conn_ref = uuid.UUID(str(connector_id))
        except ValueError:
            _conn_ref = None
        vars_ctx = case_vars.CallerContext(
            kind="connector", ref=_conn_ref, actor_id="service-task",
        )
        try:
            from case_service.hxbridge.protocol import get_connector
            connector = await get_connector(connector_id, session)
            if connector is None:
                raise ValueError(f"Connector '{connector_id}' not found")

            input_mapping: dict = connector_config.get("input_mapping", {})
            case_data = await case_vars.get_all(session, vars_ctx, case_id)
            payload = {k: case_data.get(v, v) for k, v in input_mapping.items()} if input_mapping else dict(case_data)
            connector_result = await connector.execute(payload) or {}
        except Exception as e:
            connector_result = {"_error": str(e)}

        # Write result back as case variables in the connector's registered
        # namespace (Phase 2). Connectors without a namespace fall back to
        # the legacy case.data blob so existing case types keep working.
        output_mapping: dict = connector_config.get("output_mapping", {})
        if output_mapping and isinstance(connector_result, dict):
            new_data: dict | None = None
            for src, dest in output_mapping.items():
                if src not in connector_result:
                    continue
                try:
                    await case_vars.set_variable(
                        session, vars_ctx, case_id, dest, connector_result[src],
                    )
                except case_vars.VariableError as ve:
                    if new_data is None:
                        logger.warning(
                            "service_task %s: case_vars write rejected (%s) — "
                            "falling back to case.data blob", connector_id, ve,
                        )
                        new_data = dict(case.data or {})
                    new_data[dest] = connector_result[src]
            if new_data is not None:
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
            case_data = await case_vars.get_all(
                session,
                case_vars.CallerContext(kind="platform", actor_id="hxfusion"),
                case_id,
            )
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


# ─── Rule application (Case Variables Phase 3) ───────────────────────


async def apply_rule_actions(
    session: AsyncSession,
    case_id: uuid.UUID,
    action_results: list[dict],
    actor_id: str,
    rule_id: str,
) -> list[dict]:
    """Apply a matched rule's set_value actions to case storage.

    Routing per target:
      velaris.status / velaris.priority  → full lifecycle machinery
      other velaris.*                    → rejected (virtual, read-only)
      namespace.name                     → grant-checked case_vars write
                                           (rules own no namespace — needs a
                                           namespace_grants row, ref = rule id
                                           or the "*" wildcard)
      bare names                         → context-only, not persisted
    """
    applied: list[dict] = []
    # actor_id is the bare rule UUID — namespace_grants rows for rules use
    # grantee_ref = <rule uuid> (or "*"), and written_by records "<ns>:<uuid>"
    rules_ctx = case_vars.CallerContext(kind="rules", actor_id=str(rule_id))
    for ar in action_results or []:
        if ar.get("action") != "set_value":
            continue
        target, value = ar.get("target") or "", ar.get("value")
        entry: dict = {"target": target, "value": value}
        try:
            if target == "velaris.status":
                await apply_status_change(
                    session, case_id, str(value), actor_id,
                    reason=f"rule:{rule_id}",
                )
                entry["applied"] = "lifecycle:status"
            elif target == "velaris.priority":
                await apply_priority_change(session, case_id, str(value), actor_id)
                entry["applied"] = "lifecycle:priority"
            elif target.startswith("velaris."):
                raise case_vars.VariableError(
                    f"'{target}' is a virtual projection — only velaris.status "
                    "and velaris.priority are writable, via the lifecycle."
                )
            elif "." in target:
                result = await case_vars.set_granted(
                    session, rules_ctx, case_id, target, value,
                )
                entry["applied"] = f"variable:{result['full_key']}"
            else:
                entry["applied"] = "context-only"
        except case_vars.VariableError as ve:
            entry["error"] = str(ve)
        applied.append(entry)
    return applied


@router.post("/{case_id}/rules/{rule_id}/apply")
async def apply_rule_to_case(
    case_id: uuid.UUID,
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Evaluate a stored rule against this case and apply its actions.

    Context is built from the case_vars façade (rules context, redacted),
    so conditions reference variables as ``crm.account_status`` or
    ``case.data.crm.account_status``; velaris.* projections are readable.
    """
    from case_service.core.rules_evaluator import evaluate_rule as _eval_rule
    from case_service import hxguard

    # HxGuard pilot: any authenticated user today, but the decision (and any
    # future tightening) lives in the PDP, not in this route.
    await hxguard.require(
        session, hxguard.subject_from_user(user), "case.rule.apply",
        resource={"id": str(case_id)},
    )
    # Phase B: applying a rule mutates the case — case.update in shadow mode
    await hxguard.require_case(session, user, "case.update", case_id)

    case = await _get_case_or_404(session, case_id)
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    if not rule.enabled:
        raise HTTPException(409, "Rule is disabled")
    # scope enforcement: a case_type-scoped rule only applies to its own type
    if rule.scope == "case_type" and rule.scope_target_id \
            and rule.scope_target_id != str(case.case_type_id):
        raise HTTPException(403, "Rule is scoped to a different case type")

    rules_ctx = case_vars.CallerContext(kind="rules", actor_id=f"rule:{rule_id}")
    vars_dict = await case_vars.get_all(session, rules_ctx, case_id)
    # both shapes resolve: bare "crm.x" and "case.data.crm.x" / "case.status"
    context = {
        **vars_dict,
        "case": {
            "id": str(case.id),
            "status": case.status,
            "priority": case.priority,
            "current_stage_id": case.current_stage_id,
            "data": vars_dict,
        },
    }

    rule_dict = {
        "id": str(rule.id), "name": rule.name,
        "rule_type": rule.rule_type, "priority": rule.priority,
        **(rule.definition_json or {}),
    }
    result = _eval_rule(rule_dict, context)

    applied: list[dict] = []
    if result.get("matched"):
        applied = await apply_rule_actions(
            session, case_id, result.get("action_results") or [],
            actor_id=user.user_id, rule_id=str(rule.id),
        )

    return {
        "case_id": str(case_id),
        "rule_id": str(rule.id),
        "matched": result.get("matched", False),
        "action_results": result.get("action_results"),
        "applied": applied,
    }


# ─── Case sharing (HxGuard Phase B) ──────────────────────────────────


class ShareCreate(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    relation: str = "viewer"     # viewer | editor


async def _require_share_access(session, user, case_id: uuid.UUID, action: str) -> None:
    """Sharing endpoints are NEW surfaces — always enforced, regardless of
    the shadow-mode setting that protects pre-existing case routes.

    Anti-oracle: a caller who cannot READ the case gets 404 (existence is
    not confirmed); 403 is reserved for callers who can read but lack the
    requested capability."""
    from case_service import hxguard
    subject = hxguard.subject_from_user(user)
    decision = await hxguard.check(session, subject, action, {"case_id": case_id})
    if decision.allow:
        return
    can_read = (await hxguard.check(
        session, subject, "case.read", {"case_id": case_id},
    )).allow
    if not can_read:
        raise HTTPException(404, "Case not found")
    raise HTTPException(403, f"Not authorized: {decision.reason}")


@router.post("/{case_id}/shares", status_code=201)
async def share_case(
    case_id: uuid.UUID,
    body: ShareCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if body.relation not in ("viewer", "editor"):
        raise HTTPException(400, "relation must be viewer | editor")
    await _get_case_or_404(session, case_id)
    await _require_share_access(session, user, case_id, "case.share")
    from case_service.hxguard import tuples as hxg_tuples
    await hxg_tuples.write_tuple(
        session, object_type="case", object_id=case_id,
        relation=body.relation, subject_type="user",
        subject_id=body.user_id, created_by=user.user_id,
    )
    await _audit(session, case_id, "case_shared", actor_id=user.user_id,
                 details={"user_id": body.user_id, "relation": body.relation})
    return {"case_id": str(case_id), "user_id": body.user_id, "relation": body.relation}


@router.get("/{case_id}/shares")
async def list_case_shares(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _get_case_or_404(session, case_id)
    await _require_share_access(session, user, case_id, "case.read")
    from case_service.hxguard import tuples as hxg_tuples
    rows = await hxg_tuples.list_tuples(
        session, object_type="case", object_id=case_id,
        relations={"viewer", "editor", "assignee"},
    )
    return [
        {"user_id": t.subject_id, "relation": t.relation,
         "created_by": t.created_by,
         "created_at": t.created_at.isoformat() if t.created_at else None}
        for t in rows
    ]


@router.delete("/{case_id}/shares", status_code=204)
async def unshare_case(
    case_id: uuid.UUID,
    user_id: str,
    relation: str = Query(..., pattern="^(viewer|editor)$"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _get_case_or_404(session, case_id)
    await _require_share_access(session, user, case_id, "case.share")
    from case_service.hxguard import tuples as hxg_tuples
    removed = await hxg_tuples.remove_tuple(
        session, object_type="case", object_id=case_id,
        relation=relation, subject_type="user", subject_id=user_id,
    )
    if not removed:
        raise HTTPException(404, "Share not found")
    await _audit(session, case_id, "case_unshared", actor_id=user.user_id,
                 details={"user_id": user_id, "relation": relation})


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
        case_description=(await case_vars.get_all(
            session,
            case_vars.CallerContext(kind="platform", actor_id=user.user_id,
                                    privileged=_has_sensitive_access(user)),
            case.id,
        )).get("description"),
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
