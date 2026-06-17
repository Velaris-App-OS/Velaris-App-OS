"""Case lifecycle orchestrator.

High-level operations that compose the core modules (assignment
routing, SLA tracking, urgency calculation, relationship management)
into coherent lifecycle actions.

These functions are called by the API routers and Temporal activities.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.core import (
    assignment_router,
    relationship_manager,
    sla_tracker,
    urgency_calculator,
)
from case_service.db import repository as repo

logger = logging.getLogger(__name__)


def _sla_uses_v2(sla_policy: dict[str, Any]) -> bool:
    """P34b — a policy opts into v2 via use_v2=true OR escalation_tree_id."""
    return bool(sla_policy.get("use_v2") or sla_policy.get("escalation_tree_id"))


async def _start_policy_sla(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_id: uuid.UUID | None,
    sla_policy: dict[str, Any],
    target_id: str,
    tenant_id: str | None = None,
):
    """Start one SLA instance, routing to v2 when the policy opts in."""
    if _sla_uses_v2(sla_policy):
        return await sla_tracker.start_sla_v2(
            session,
            case_id=case_id,
            case_type_id=case_type_id,
            sla_policy=sla_policy,
            target_id=target_id,
            tenant_id=tenant_id,
        )
    return await sla_tracker.start_sla(
        session,
        case_id=case_id,
        sla_policy=sla_policy,
        target_id=target_id,
    )


async def start_stage_slas(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_id: uuid.UUID | None,
    stage: dict[str, Any],
    case_type_def: dict[str, Any],
    tenant_id: str | None = None,
) -> int:
    """Start the SLA configured on a stage, if any and not already running.

    Chokepoint for every stage-entry path (manual transition, auto-advance,
    module-driven advance). Idempotent: re-entering a stage while its SLA
    is still active does not start a duplicate.
    """
    sla_policy_id = stage.get("sla_policy_id")
    if not sla_policy_id:
        return 0

    stage_id = stage["id"]
    existing = await repo.get_sla_instances(session, case_id)
    for sla in existing:
        if (
            sla.sla_policy_id == sla_policy_id
            and sla.target_id == stage_id
            and sla.status in ("on_track", "at_risk", "paused")
        ):
            return 0

    for sp in case_type_def.get("sla_policies", []):
        if sp["id"] == sla_policy_id:
            await _start_policy_sla(
                session,
                case_id=case_id,
                case_type_id=case_type_id,
                sla_policy=sp,
                target_id=stage_id,
                tenant_id=tenant_id,
            )
            return 1
    logger.warning(
        "Stage %s references SLA policy %s not present in case type definition",
        stage_id, sla_policy_id,
    )
    return 0


async def on_case_created(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_def: dict[str, Any],
    case_type_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
) -> None:
    """Post-creation hook: start case-level SLAs and compute initial urgency.

    Called after the case instance row is inserted, before the
    Temporal workflow begins.
    """
    # Start case-level SLAs
    for sla_policy in case_type_def.get("sla_policies", []):
        # Only case-level SLAs (not stage-scoped)
        if sla_policy.get("scope", "case") == "case":
            await _start_policy_sla(
                session,
                case_id=case_id,
                case_type_id=case_type_id,
                sla_policy=sla_policy,
                target_id=str(case_id),
                tenant_id=tenant_id,
            )

    # Initial urgency
    await urgency_calculator.refresh_urgency_for_case(session, case_id)


async def on_stage_entered(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    stage: dict[str, Any],
    case_type_def: dict[str, Any],
) -> list:
    """Called when a case enters a new stage.

    - Updates current_stage_id
    - Starts stage-level SLA
    - Creates assignments for stage steps
    - Refreshes urgency
    """
    stage_id = stage["id"]

    # Update case stage
    await repo.update_case_instance(
        session, case_id, values={"current_stage_id": stage_id}
    )

    # Start stage SLA if configured
    await start_stage_slas(
        session,
        case_id=case_id,
        case_type_id=None,
        stage=stage,
        case_type_def=case_type_def,
    )

    # Create assignments
    steps = stage.get("steps", [])
    assignments = await assignment_router.create_assignments_for_stage(
        session,
        case_id=case_id,
        stage_id=stage_id,
        steps=steps,
    )

    # Refresh urgency
    await urgency_calculator.refresh_urgency_for_case(session, case_id)

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "stage_entered",
            "actor_type": "system",
            "details": {
                "stage_id": stage_id,
                "stage_name": stage.get("name", ""),
                "assignments_created": len(assignments),
            },
        },
    )

    return assignments


async def on_step_completed(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    step_id: str,
    result: dict[str, Any] | None = None,
    completed_by: str | None = None,
) -> dict[str, Any]:
    """Called when a work item is completed.

    - Marks the assignment as completed
    - Refreshes urgency
    - Returns summary for the workflow signal
    """
    # Find and complete the active assignment for this step
    from sqlalchemy import select
    from case_service.db.models import CaseAssignmentModel

    stmt = select(CaseAssignmentModel).where(
        CaseAssignmentModel.case_id == case_id,
        CaseAssignmentModel.step_id == step_id,
        CaseAssignmentModel.status == "active",
    )
    db_result = await session.execute(stmt)
    assignment = db_result.scalar_one_or_none()

    if assignment:
        now = datetime.now(timezone.utc)
        await repo.update_assignment(
            session,
            assignment.id,
            values={"status": "completed", "completed_at": now},
        )

    await urgency_calculator.refresh_urgency_for_case(session, case_id)

    return {
        "case_id": str(case_id),
        "step_id": step_id,
        "completed_by": completed_by,
    }


async def on_status_changed(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    old_status: str,
    new_status: str,
) -> None:
    """Called after a case status change.

    - Propagates status to parent cases
    - Cancels SLAs on terminal statuses
    - Pauses/resumes SLAs based on status
    - Refreshes urgency
    """
    # Terminal statuses: cancel all SLAs
    if new_status in ("resolved", "closed", "cancelled"):
        await sla_tracker.cancel_slas_for_case(session, case_id)

    # Pending statuses: pause SLAs
    elif new_status in ("pending_external", "pending_subcase"):
        sla_instances = await repo.get_sla_instances(session, case_id)
        for sla in sla_instances:
            if sla.status in ("on_track", "at_risk"):
                await sla_tracker.pause_sla(
                    session,
                    case_id=case_id,
                    sla_policy_id=sla.sla_policy_id,
                )

    # Resuming from pending: resume SLAs
    elif new_status in ("open", "reopened") and old_status in (
        "pending_external",
        "pending_subcase",
    ):
        sla_instances = await repo.get_sla_instances(session, case_id)
        for sla in sla_instances:
            if sla.status == "paused":
                await sla_tracker.resume_sla(
                    session,
                    case_id=case_id,
                    sla_policy_id=sla.sla_policy_id,
                )

    # Propagate to parents
    await relationship_manager.propagate_status_change(
        session, case_id=case_id, new_status=new_status
    )

    # Refresh urgency
    await urgency_calculator.refresh_urgency_for_case(session, case_id)


async def on_priority_changed(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    new_priority: str,
) -> None:
    """Called after a case priority change.

    - Propagates priority to parent cases
    - Refreshes urgency
    """
    await relationship_manager.propagate_priority_change(
        session, case_id=case_id, new_priority=new_priority
    )
    await urgency_calculator.refresh_urgency_for_case(session, case_id)
