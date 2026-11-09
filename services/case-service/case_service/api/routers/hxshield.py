"""P59 HxShield — Case Fraud & Abuse Detection API."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from velaris_shared.auth.models import AuthenticatedUser
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import SecurityRuleModel, SecurityIncidentModel, ShieldEventModel
from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared.db.session import get_session
from case_service.api.schemas.hxshield import (
    SecurityRuleCreate, SecurityRuleUpdate, SecurityRuleOut,
    SecurityIncidentOut, IncidentResolve,
    ShieldEventOut, ScoreRequest, ScoreResponse,
)
from velaris_shared.db.read_routing import get_read_session

router = APIRouter(prefix="/shield", tags=["HxShield"], dependencies=[Depends(hxguard.guard("security.shield.access"))])


# ── Security Rules ─────────────────────────────────────────────────────────────

@router.get("/rules", response_model=list[SecurityRuleOut])
async def list_rules(
    enabled: bool | None = None,
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    q = select(SecurityRuleModel)
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    q = await scope_to_caller(q, SecurityRuleModel, session, user)
    if enabled is not None:
        q = q.where(SecurityRuleModel.enabled == enabled)
    if tenant_id:
        q = q.where(SecurityRuleModel.tenant_id == tenant_id)
    result = await session.execute(q.order_by(SecurityRuleModel.created_at.desc()))
    return result.scalars().all()


@router.post("/rules", response_model=SecurityRuleOut, status_code=201)
async def create_rule(
    body: SecurityRuleCreate,
    session: AsyncSession = Depends(get_session),
):
    rule = SecurityRuleModel(**body.model_dump())
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


@router.get("/rules/{rule_id}", response_model=SecurityRuleOut)
async def get_rule(rule_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    rule = await session.get(SecurityRuleModel, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    return rule


@router.patch("/rules/{rule_id}", response_model=SecurityRuleOut)
async def update_rule(
    rule_id: uuid.UUID,
    body: SecurityRuleUpdate,
    session: AsyncSession = Depends(get_session),
):
    rule = await session.get(SecurityRuleModel, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(rule, k, v)
    await session.commit()
    await session.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    rule = await session.get(SecurityRuleModel, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    await session.delete(rule)
    await session.commit()


# ── Security Incidents ─────────────────────────────────────────────────────────

@router.get("/incidents", response_model=list[SecurityIncidentOut])
async def list_incidents(
    status: str | None = None,
    severity: str | None = None,
    tenant_id: str | None = None,
    actor_id: str | None = None,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    q = select(SecurityIncidentModel)
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    q = await scope_to_caller(q, SecurityIncidentModel, session, user)
    if status:
        q = q.where(SecurityIncidentModel.status == status)
    if severity:
        q = q.where(SecurityIncidentModel.severity == severity)
    if tenant_id:
        q = q.where(SecurityIncidentModel.tenant_id == tenant_id)
    if actor_id:
        q = q.where(SecurityIncidentModel.actor_id == actor_id)
    result = await session.execute(q.order_by(SecurityIncidentModel.detected_at.desc()).limit(limit))
    return result.scalars().all()


@router.get("/incidents/{incident_id}", response_model=SecurityIncidentOut)
async def get_incident(incident_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    inc = await session.get(SecurityIncidentModel, incident_id)
    if not inc:
        raise HTTPException(404, "Incident not found")
    return inc


@router.post("/incidents/{incident_id}/resolve", response_model=SecurityIncidentOut)
async def resolve_incident(
    incident_id: uuid.UUID,
    body: IncidentResolve,
    session: AsyncSession = Depends(get_session),
):
    inc = await session.get(SecurityIncidentModel, incident_id)
    if not inc:
        raise HTTPException(404, "Incident not found")
    inc.status = "resolved"
    inc.resolved_at = datetime.now(timezone.utc)
    inc.resolved_by = body.resolved_by
    await session.commit()
    await session.refresh(inc)
    return inc


@router.post("/incidents/{incident_id}/dismiss", response_model=SecurityIncidentOut)
async def dismiss_incident(
    incident_id: uuid.UUID,
    body: IncidentResolve,
    session: AsyncSession = Depends(get_session),
):
    inc = await session.get(SecurityIncidentModel, incident_id)
    if not inc:
        raise HTTPException(404, "Incident not found")
    inc.status = "dismissed"
    inc.resolved_at = datetime.now(timezone.utc)
    inc.resolved_by = body.resolved_by
    await session.commit()
    await session.refresh(inc)
    return inc


# ── Shield Events ──────────────────────────────────────────────────────────────

@router.get("/events", response_model=list[ShieldEventOut])
async def list_events(
    actor_id: str | None = None,
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(100, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    q = select(ShieldEventModel).where(ShieldEventModel.score >= min_score)
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    q = await scope_to_caller(q, ShieldEventModel, session, user)
    if actor_id:
        q = q.where(ShieldEventModel.actor_id == actor_id)
    result = await session.execute(q.order_by(ShieldEventModel.recorded_at.desc()).limit(limit))
    return result.scalars().all()


# ── Score (manual evaluation) ─────────────────────────────────────────────────

@router.post("/score", response_model=ScoreResponse)
async def score_event(
    body: ScoreRequest,
    session: AsyncSession = Depends(get_session),
):
    from case_service.hxshield.engine import evaluate
    result = await evaluate(
        event_type=body.event_type,
        actor_id=body.actor_id,
        tenant_id=body.tenant_id,
        case_type_id=body.case_type_id,
        context=body.context,
        session=session,
    )
    await session.commit()
    return ScoreResponse(**result)


# ── Stats ──────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_read_session)):
    from sqlalchemy import func
    open_q = await session.execute(
        select(func.count()).select_from(SecurityIncidentModel)
        .where(SecurityIncidentModel.status == "open")
    )
    total_q = await session.execute(
        select(func.count()).select_from(SecurityIncidentModel)
    )
    by_sev = await session.execute(
        select(SecurityIncidentModel.severity, func.count().label("c"))
        .group_by(SecurityIncidentModel.severity)
    )
    events_q = await session.execute(
        select(func.count()).select_from(ShieldEventModel)
        .where(ShieldEventModel.score > 0)
    )
    return {
        "open_incidents": open_q.scalar(),
        "total_incidents": total_q.scalar(),
        "flagged_events": events_q.scalar(),
        "by_severity": {row.severity: row.c for row in by_sev},
    }


# ── Responses (roadmap #25) ────────────────────────────────────────────────────
#
# The half that acts. Until 2026-09-09 the engine computed an action for every
# incident — flag, alert, suspend, block — and nothing performed one. These
# routes are the queue where a person decides, and the module's own docstring
# explains why almost everything here is about declining to act.
#
# The whole router is already gated on `security.shield.access`. Approving a
# response needs MORE than reading the queue, so the two destructive routes ask
# for a second privilege on top.

def _visible_to(user) -> set:
    """The tenant keys a caller may see and act on in the response queue.

    A row carries the tenant the incident was detected in, and a row with NONE
    is platform scope — which is most of them, because a security event does not
    carry a tenant and the detector only has one when the call site passed it.

    So: your own tenant, plus the un-scoped rows, and never another tenant's.
    Without this a tenant administrator could **approve a suspension of another
    tenant's user** — the queue is not a report, its buttons act.
    """
    return {getattr(user, "tenant_id", None), None}


async def _visible_keys(session, user) -> set:
    """`_visible_to`, with platform scope (no tenant) for the platform operator
    ONLY (2026-11-04). Every tenant's admin — a website demo account among
    them — saw the platform-scope rows, which are most of them, and could
    APPROVE one: revoke the sessions of, or suspend, the operator or another
    tenant's user."""
    from case_service.tenancy.identity import is_platform_operator
    keys = {getattr(user, "tenant_id", None)}
    if await is_platform_operator(session, user):
        keys.add(None)
    return keys


async def _own_response(session, response_id, user):
    from velaris_shared.db.models import ShieldResponseModel

    row = await session.get(ShieldResponseModel, response_id)
    if row is None:
        raise HTTPException(404, "Response not found")
    if row.tenant_id not in await _visible_keys(session, user):
        # 404, not 403: a 403 confirms the row exists, and the row names an
        # actor in another tenant.
        raise HTTPException(404, "Response not found")
    return row


@router.get("/responses")
async def list_responses(
    state: str | None = Query(None, description="proposed | approved | executed | failed | declined | shadowed"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
):
    """The queue. Newest first, because the one that matters is the last one."""
    from velaris_shared.db.models import ShieldResponseModel
    from case_service.hxshield.response import catalogue, responses_enabled

    keys = await _visible_keys(session, user)
    cond = ShieldResponseModel.tenant_id.in_([t for t in keys if t])
    if None in keys:
        cond = cond | ShieldResponseModel.tenant_id.is_(None)
    stmt = select(ShieldResponseModel).where(cond).order_by(
        ShieldResponseModel.created_at.desc()).limit(limit)
    if state:
        stmt = stmt.where(ShieldResponseModel.state == state)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        # Said on every read, not buried in a settings screen: a queue full of
        # "shadowed" rows is not a backlog somebody forgot, it is the platform
        # working as configured, and an operator must be able to tell those
        # apart at a glance.
        "responses_enabled": responses_enabled(),
        "catalogue": catalogue(),
        "items": [
            {
                "id": str(r.id),
                "incident_id": str(r.incident_id) if r.incident_id else None,
                "response_type": r.response_type,
                "target_kind": r.target_kind,
                "target_id": r.target_id,
                "state": r.state,
                "destructive": r.destructive,
                "reason": r.reason,
                "score": r.score,
                "approved_by": r.approved_by,
                "approved_at": r.approved_at.isoformat() if r.approved_at else None,
                "declined_by": r.declined_by,
                "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                "result": r.result,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/responses/{response_id}/approve")
async def approve_response(
    response_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(hxguard.guard("security.shield.respond")),
):
    """Approve and run one response.

    A separate privilege from reading the queue: seeing that the platform wants
    to suspend somebody and being able to suspend them are different jobs on
    most floors, and the second is the one that stops a person working.
    """
    from case_service.hxshield.response import approve

    row = await _own_response(session, response_id, user)
    actor = str(getattr(user, "user_id", None) or getattr(user, "id", "") or "unknown")
    try:
        result = await approve(session, row, actor=actor)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return {"id": str(row.id), "state": row.state, "result": result}


@router.post("/responses/{response_id}/decline")
async def decline_response(
    response_id: uuid.UUID,
    body: dict | None = None,
    session: AsyncSession = Depends(get_session),
    user=Depends(hxguard.guard("security.shield.respond")),
):
    """Say no, on the record.

    A declined proposal is evidence about the DETECTOR: a rule declined every
    time is a rule that should change, and that only becomes visible if "no" is
    written down as firmly as "yes".
    """
    from case_service.hxshield.response import decline

    row = await _own_response(session, response_id, user)
    actor = str(getattr(user, "user_id", None) or getattr(user, "id", "") or "unknown")
    try:
        await decline(session, row, actor=actor,
                      reason=(body or {}).get("reason"))
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return {"id": str(row.id), "state": row.state}
