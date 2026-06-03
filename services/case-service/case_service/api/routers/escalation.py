"""Escalation tree + SLA v2 router."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.escalation import (
    EscalationTreeCreate, EscalationTreeUpdate, EscalationTreeResponse,
    EscalationPreviewRequest, SLAPauseRequest, SLAResumeRequest,
)
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.core.sla_escalation import (
    precompute_level_schedule, record_pause_with_reason, record_resume,
    apply_level, resolve_escalation_tree, snapshot_tree,
)
from case_service.core.sla_tracker import parse_iso8601_duration
from case_service.db.models import CaseSLAInstanceModel, EscalationTreeModel
from case_service.db.session import get_session

router = APIRouter(tags=["escalation"])


def _tree_to_response(t: EscalationTreeModel) -> EscalationTreeResponse:
    return EscalationTreeResponse(
        id=t.id, name=t.name, description=t.description or "",
        scope=t.scope, case_type_id=t.case_type_id, tenant_id=t.tenant_id,
        tree_json=t.tree_json or {}, is_active=t.is_active,
        created_by=t.created_by,
        created_at=t.created_at.isoformat() if t.created_at else "",
        updated_at=t.updated_at.isoformat() if t.updated_at else "",
    )


# ── Escalation tree CRUD ─────────────────────────────────────────────

@router.post("/escalation-trees", response_model=EscalationTreeResponse, status_code=201)
async def create_tree(
    body: EscalationTreeCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if body.scope == "case_type" and body.case_type_id is None:
        raise HTTPException(400, "case_type_id required when scope='case_type'")
    tree = EscalationTreeModel(
        name=body.name, description=body.description,
        scope=body.scope, case_type_id=body.case_type_id,
        tenant_id=user.tenant_id or body.tenant_id,
        tree_json=body.tree_json.model_dump(), is_active=body.is_active,
        created_by=user.user_id,
    )
    session.add(tree)
    await session.flush()
    return _tree_to_response(tree)


@router.get("/escalation-trees", response_model=list[EscalationTreeResponse])
async def list_trees(
    scope: Optional[str] = Query(None),
    case_type_id: Optional[uuid.UUID] = Query(None),
    tenant_id: Optional[str] = Query(None),
    active_only: bool = Query(True),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if user.tenant_id and "superadmin" not in (user.roles or []):
        tenant_id = user.tenant_id
    q = select(EscalationTreeModel)
    if scope:
        q = q.where(EscalationTreeModel.scope == scope)
    if case_type_id:
        q = q.where(EscalationTreeModel.case_type_id == case_type_id)
    if tenant_id:
        q = q.where(EscalationTreeModel.tenant_id == tenant_id)
    if active_only:
        q = q.where(EscalationTreeModel.is_active.is_(True))
    q = q.order_by(EscalationTreeModel.updated_at.desc())
    res = await session.execute(q)
    return [_tree_to_response(t) for t in res.scalars().all()]


@router.get("/escalation-trees/{tree_id}", response_model=EscalationTreeResponse)
async def get_tree(tree_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    t = await session.get(EscalationTreeModel, tree_id)
    if t is None:
        raise HTTPException(404, "Tree not found")
    return _tree_to_response(t)


@router.patch("/escalation-trees/{tree_id}", response_model=EscalationTreeResponse)
async def update_tree(
    tree_id: uuid.UUID,
    body: EscalationTreeUpdate,
    session: AsyncSession = Depends(get_session),
):
    t = await session.get(EscalationTreeModel, tree_id)
    if t is None:
        raise HTTPException(404, "Tree not found")
    if body.name is not None: t.name = body.name
    if body.description is not None: t.description = body.description
    if body.tree_json is not None: t.tree_json = body.tree_json.model_dump()
    if body.is_active is not None: t.is_active = body.is_active
    await session.flush()
    return _tree_to_response(t)


@router.delete("/escalation-trees/{tree_id}", status_code=204)
async def delete_tree(tree_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    t = await session.get(EscalationTreeModel, tree_id)
    if t:
        t.is_active = False
        await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


# ── Preview: compute level schedule for given tree + durations ──────

@router.post("/escalation-trees/preview")
async def preview_tree(
    body: EscalationPreviewRequest,
):
    try:
        started = (
            datetime.fromisoformat(body.started_at).replace(tzinfo=timezone.utc)
            if body.started_at else datetime.now(timezone.utc)
        )
        goal_at = started + parse_iso8601_duration(body.goal_duration)
        deadline_at = started + parse_iso8601_duration(body.deadline_duration)
    except ValueError as e:
        raise HTTPException(400, f"invalid duration: {e}")

    snapshot = {"levels": body.tree_json.model_dump()["levels"]}
    schedule = precompute_level_schedule(snapshot, started, goal_at, deadline_at)
    return {
        "started_at": started.isoformat(),
        "goal_at": goal_at.isoformat(),
        "deadline_at": deadline_at.isoformat(),
        "schedule": schedule,
    }


# ── SLA v2: pause-with-reason / resume / manual-escalate ────────────

@router.post("/cases/{case_id}/sla/{policy_id}/pause-v2")
async def pause_sla_with_reason(
    case_id: uuid.UUID,
    policy_id: str,
    body: SLAPauseRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    q = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == policy_id,
    )
    res = await session.execute(q)
    sla = res.scalar_one_or_none()
    if sla is None:
        raise HTTPException(404, "SLA not found")
    if sla.status == "paused":
        raise HTTPException(409, "SLA already paused")

    sla.status = "paused"
    sla.paused_at = datetime.now(timezone.utc)
    await record_pause_with_reason(session, sla, body.reason, body.actor_id or user.user_id)
    return {
        "status": "paused",
        "reason": sla.pause_reason,
        "pause_log_entries": len(sla.pause_reasons_log or []),
    }


@router.post("/cases/{case_id}/sla/{policy_id}/resume-v2")
async def resume_sla_v2(
    case_id: uuid.UUID,
    policy_id: str,
    body: SLAResumeRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    q = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == policy_id,
    )
    res = await session.execute(q)
    sla = res.scalar_one_or_none()
    if sla is None:
        raise HTTPException(404, "SLA not found")
    if sla.status != "paused":
        raise HTTPException(409, "SLA is not paused")
    sla.status = "on_track"
    sla.paused_at = None
    await record_resume(session, sla, body.actor_id or user.user_id)
    return {"status": "on_track"}


@router.post("/cases/{case_id}/sla/{policy_id}/escalate")
async def manual_escalate(
    case_id: uuid.UUID,
    policy_id: str,
    level: int = Query(..., ge=1),
    session: AsyncSession = Depends(get_session),
):
    q = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == policy_id,
    )
    res = await session.execute(q)
    sla = res.scalar_one_or_none()
    if sla is None:
        raise HTTPException(404, "SLA not found")

    schedule = (sla.escalation_tree_snapshot or {}).get("schedule", [])
    level_entry = next((e for e in schedule if e.get("level") == level), None)
    if level_entry is None:
        raise HTTPException(400, f"level {level} not found in escalation schedule")

    result = await apply_level(session, case_id, sla, level_entry)
    return result


@router.get("/cases/{case_id}/sla/{policy_id}/escalation-schedule")
async def get_schedule(
    case_id: uuid.UUID,
    policy_id: str,
    session: AsyncSession = Depends(get_session),
):
    q = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == policy_id,
    )
    res = await session.execute(q)
    sla = res.scalar_one_or_none()
    if sla is None:
        raise HTTPException(404, "SLA not found")
    snap = sla.escalation_tree_snapshot or {}
    return {
        "current_level": sla.escalation_level,
        "schedule": snap.get("schedule", []),
        "history": sla.escalation_history or [],
        "pause_log": sla.pause_reasons_log or [],
    }
