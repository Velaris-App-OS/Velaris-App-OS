"""P59 HxShield — Case Fraud & Abuse Detection API."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import SecurityRuleModel, SecurityIncidentModel, ShieldEventModel
from case_service.auth.dependencies import require_role
from case_service.db.session import get_session
from case_service.api.schemas.hxshield import (
    SecurityRuleCreate, SecurityRuleUpdate, SecurityRuleOut,
    SecurityIncidentOut, IncidentResolve,
    ShieldEventOut, ScoreRequest, ScoreResponse,
)

router = APIRouter(prefix="/shield", tags=["HxShield"], dependencies=[Depends(require_role("security", "admin"))])


# ── Security Rules ─────────────────────────────────────────────────────────────

@router.get("/rules", response_model=list[SecurityRuleOut])
async def list_rules(
    enabled: bool | None = None,
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(SecurityRuleModel)
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
):
    q = select(SecurityIncidentModel)
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
):
    q = select(ShieldEventModel).where(ShieldEventModel.score >= min_score)
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
async def stats(session: AsyncSession = Depends(get_session)):
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
