"""P35 HxGlobal — Multi-Region & Data Sovereignty API router."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    RegionModel,
    SovereigntyRuleModel,
    TenantRegionAssignmentModel,
    RegionHealthLogModel,
    RegionAccessLogModel,
)
from case_service.db.session import get_session
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.hxglobal import regions as _reg  # noqa: F401
from case_service.hxglobal.protocol import get_region_adapter
from case_service.hxglobal.health import poll_region_health
from case_service.hxglobal.migration import migrate_tenant
from case_service.hxglobal.sovereignty import resolve_region, log_access

router = APIRouter(prefix="/global", tags=["hxglobal"])

PROVIDERS = ["local", "aws", "gcp", "azure"]
REGULATIONS = ["GDPR", "HIPAA", "CCPA", "PDPA", "SOC2"]
ASSIGNMENT_TYPES = ["primary", "replica", "readonly"]


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CreateRegionRequest(BaseModel):
    name: str
    provider: str = "local"
    location: Optional[str] = None
    endpoint: Optional[str] = None
    connection_config: dict = {}
    is_primary: bool = False

class UpdateRegionRequest(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    endpoint: Optional[str] = None
    connection_config: Optional[dict] = None
    is_primary: Optional[bool] = None
    enabled: Optional[bool] = None

class CreateSovereigntyRuleRequest(BaseModel):
    tenant_id: Optional[str] = None
    case_type_id: Optional[str] = None
    region_id: uuid.UUID
    regulation: str = "GDPR"
    description: Optional[str] = None

class UpdateSovereigntyRuleRequest(BaseModel):
    tenant_id: Optional[str] = None
    case_type_id: Optional[str] = None
    region_id: Optional[uuid.UUID] = None
    regulation: Optional[str] = None
    description: Optional[str] = None

class UpdateAssignmentRequest(BaseModel):
    assignment_type: Optional[str] = None

class CreateAssignmentRequest(BaseModel):
    tenant_id: str
    region_id: uuid.UUID
    assignment_type: str = "primary"

class MigrateTenantRequest(BaseModel):
    tenant_id: str
    target_region_id: uuid.UUID

class ResolveRegionRequest(BaseModel):
    tenant_id: Optional[str] = None
    case_type_id: Optional[str] = None


# ── Serialisers ───────────────────────────────────────────────────────────────

def _region(r: RegionModel) -> dict:
    return {
        "id": str(r.id), "name": r.name, "provider": r.provider,
        "location": r.location, "endpoint": r.endpoint,
        "connection_config": r.connection_config,
        "is_primary": r.is_primary, "enabled": r.enabled,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }

def _rule(r: SovereigntyRuleModel) -> dict:
    return {
        "id": str(r.id), "tenant_id": r.tenant_id, "case_type_id": r.case_type_id,
        "region_id": str(r.region_id), "regulation": r.regulation,
        "description": r.description, "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }

def _assignment(a: TenantRegionAssignmentModel) -> dict:
    return {
        "id": str(a.id), "tenant_id": a.tenant_id, "region_id": str(a.region_id),
        "assignment_type": a.assignment_type,
        "migrated_at": a.migrated_at.isoformat() if a.migrated_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }

def _health_log(h: RegionHealthLogModel) -> dict:
    return {
        "id": str(h.id), "region_id": str(h.region_id), "status": h.status,
        "latency_ms": h.latency_ms, "active_cases": h.active_cases,
        "replication_lag_ms": h.replication_lag_ms, "error_msg": h.error_msg,
        "recorded_at": h.recorded_at.isoformat() if h.recorded_at else None,
    }

def _access_log(a: RegionAccessLogModel) -> dict:
    return {
        "id": str(a.id), "region_id": str(a.region_id), "tenant_id": a.tenant_id,
        "actor_id": a.actor_id, "action": a.action, "resource": a.resource,
        "legal_basis": a.legal_basis,
        "recorded_at": a.recorded_at.isoformat() if a.recorded_at else None,
    }


# ── Regions CRUD ──────────────────────────────────────────────────────────────

@router.get("/regions")
async def list_regions(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    rows = (await session.execute(select(RegionModel).order_by(RegionModel.created_at))).scalars().all()
    return {"regions": [_region(r) for r in rows], "total": len(rows)}


@router.post("/regions", status_code=201)
async def create_region(
    body: CreateRegionRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    if body.provider not in PROVIDERS:
        raise HTTPException(400, f"Invalid provider. Must be one of: {PROVIDERS}")
    r = RegionModel(
        name=body.name, provider=body.provider, location=body.location,
        endpoint=body.endpoint, connection_config=body.connection_config,
        is_primary=body.is_primary,
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return _region(r)


@router.get("/regions/{region_id}")
async def get_region(
    region_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    return _region(await _region_or_404(session, region_id))


@router.patch("/regions/{region_id}")
async def update_region(
    region_id: uuid.UUID,
    body: UpdateRegionRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _region_or_404(session, region_id)
    if body.name is not None:              r.name = body.name
    if body.location is not None:          r.location = body.location
    if body.endpoint is not None:          r.endpoint = body.endpoint
    if body.connection_config is not None: r.connection_config = body.connection_config
    if body.is_primary is not None:        r.is_primary = body.is_primary
    if body.enabled is not None:           r.enabled = body.enabled
    r.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(r)
    return _region(r)


@router.delete("/regions/{region_id}", status_code=204)
async def delete_region(
    region_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _region_or_404(session, region_id)
    await session.delete(r)
    await session.commit()


@router.post("/regions/{region_id}/ping")
async def ping_region(
    region_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _region_or_404(session, region_id)
    adapter = get_region_adapter(r.provider, r.connection_config)
    return adapter.ping()


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health")
async def global_health(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    regions = (await session.execute(
        select(RegionModel).where(RegionModel.enabled == True)  # noqa: E712
    )).scalars().all()
    results = []
    for region in regions:
        summary = await poll_region_health(region, session)
        results.append(summary)
    await session.commit()
    return {"health": results, "total": len(results)}


@router.get("/health/{region_id}/history")
async def region_health_history(
    region_id: uuid.UUID,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    await _region_or_404(session, region_id)
    rows = (await session.execute(
        select(RegionHealthLogModel)
        .where(RegionHealthLogModel.region_id == region_id)
        .order_by(desc(RegionHealthLogModel.recorded_at))
        .limit(limit)
    )).scalars().all()
    return {"history": [_health_log(h) for h in rows], "total": len(rows)}


# ── Sovereignty rules ─────────────────────────────────────────────────────────

@router.get("/sovereignty-rules")
async def list_sovereignty_rules(
    tenant_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    q = select(SovereigntyRuleModel).order_by(SovereigntyRuleModel.created_at)
    if tenant_id:
        q = q.where(SovereigntyRuleModel.tenant_id == tenant_id)
    rows = (await session.execute(q)).scalars().all()
    return {"rules": [_rule(r) for r in rows], "total": len(rows)}


@router.post("/sovereignty-rules", status_code=201)
async def create_sovereignty_rule(
    body: CreateSovereigntyRuleRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _region_or_404(session, body.region_id)
    if body.regulation not in REGULATIONS:
        raise HTTPException(400, f"regulation must be one of: {REGULATIONS}")
    r = SovereigntyRuleModel(
        tenant_id=body.tenant_id, case_type_id=body.case_type_id,
        region_id=body.region_id, regulation=body.regulation,
        description=body.description, created_by=user.user_id,
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return _rule(r)


@router.patch("/sovereignty-rules/{rule_id}")
async def update_sovereignty_rule(
    rule_id: uuid.UUID,
    body: UpdateSovereigntyRuleRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await session.get(SovereigntyRuleModel, rule_id)
    if not r:
        raise HTTPException(404, "Sovereignty rule not found")
    if body.tenant_id is not None:
        r.tenant_id = body.tenant_id or None
    if body.case_type_id is not None:
        r.case_type_id = body.case_type_id or None
    if body.region_id is not None:
        await _region_or_404(session, body.region_id)
        r.region_id = body.region_id
    if body.regulation is not None:
        if body.regulation not in REGULATIONS:
            raise HTTPException(400, f"regulation must be one of: {REGULATIONS}")
        r.regulation = body.regulation
    if body.description is not None:
        r.description = body.description or None
    await session.commit()
    await session.refresh(r)
    return _rule(r)


@router.delete("/sovereignty-rules/{rule_id}", status_code=204)
async def delete_sovereignty_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await session.get(SovereigntyRuleModel, rule_id)
    if not r:
        raise HTTPException(404, "Sovereignty rule not found")
    await session.delete(r)
    await session.commit()


@router.post("/sovereignty-rules/resolve")
async def resolve_sovereignty(
    body: ResolveRegionRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return which region applies for this tenant + case type."""
    region = await resolve_region(body.tenant_id, body.case_type_id, session)
    if region:
        await log_access(region.id, session, tenant_id=body.tenant_id,
                         actor_id=user.user_id, action="resolve_region",
                         resource=body.case_type_id, legal_basis="legitimate_interest")
        await session.commit()
    return {
        "tenant_id": body.tenant_id, "case_type_id": body.case_type_id,
        "region": _region(region) if region else None,
    }


# ── Tenant region assignments ─────────────────────────────────────────────────

@router.get("/tenant-assignments")
async def list_tenant_assignments(
    tenant_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    q = select(TenantRegionAssignmentModel).order_by(TenantRegionAssignmentModel.created_at)
    if tenant_id:
        q = q.where(TenantRegionAssignmentModel.tenant_id == tenant_id)
    rows = (await session.execute(q)).scalars().all()
    return {"assignments": [_assignment(a) for a in rows], "total": len(rows)}


@router.post("/tenant-assignments", status_code=201)
async def create_tenant_assignment(
    body: CreateAssignmentRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    await _region_or_404(session, body.region_id)
    if body.assignment_type not in ASSIGNMENT_TYPES:
        raise HTTPException(400, f"assignment_type must be one of: {ASSIGNMENT_TYPES}")
    a = TenantRegionAssignmentModel(
        tenant_id=body.tenant_id, region_id=body.region_id,
        assignment_type=body.assignment_type,
    )
    session.add(a)
    try:
        await session.commit()
    except Exception as exc:
        err = str(exc).lower()
        if "unique" in err or "uq_tra" in err:
            raise HTTPException(409, "Assignment already exists for this tenant/region/type")
        raise HTTPException(500, str(exc))
    await session.refresh(a)
    return _assignment(a)


@router.patch("/tenant-assignments/{assignment_id}")
async def update_tenant_assignment(
    assignment_id: uuid.UUID,
    body: UpdateAssignmentRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy.exc import IntegrityError
    a = await session.get(TenantRegionAssignmentModel, assignment_id)
    if not a:
        raise HTTPException(404, "Assignment not found")
    if body.assignment_type is not None:
        if body.assignment_type not in ASSIGNMENT_TYPES:
            raise HTTPException(400, f"assignment_type must be one of: {ASSIGNMENT_TYPES}")
        a.assignment_type = body.assignment_type
    try:
        await session.commit()
        await session.refresh(a)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, f"An assignment of type '{body.assignment_type}' already exists for this tenant and region.")
    return _assignment(a)


@router.delete("/tenant-assignments/{assignment_id}", status_code=204)
async def delete_tenant_assignment(
    assignment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    a = await session.get(TenantRegionAssignmentModel, assignment_id)
    if not a:
        raise HTTPException(404, "Assignment not found")
    await session.delete(a)
    await session.commit()


# ── Tenant migration ──────────────────────────────────────────────────────────

@router.post("/migrate-tenant")
async def migrate_tenant_endpoint(
    body: MigrateTenantRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _region_or_404(session, body.target_region_id)
    return await migrate_tenant(body.tenant_id, body.target_region_id, session, actor_id=user.user_id)


# ── Access log ────────────────────────────────────────────────────────────────

@router.get("/access-log")
async def get_access_log(
    region_id: Optional[uuid.UUID] = None,
    tenant_id: Optional[str] = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    q = select(RegionAccessLogModel).order_by(desc(RegionAccessLogModel.recorded_at)).limit(limit)
    if region_id:
        q = q.where(RegionAccessLogModel.region_id == region_id)
    if tenant_id:
        q = q.where(RegionAccessLogModel.tenant_id == tenant_id)
    rows = (await session.execute(q)).scalars().all()
    return {"logs": [_access_log(a) for a in rows], "total": len(rows)}


# ── Helper ────────────────────────────────────────────────────────────────────

async def _region_or_404(session: AsyncSession, region_id: uuid.UUID) -> RegionModel:
    r = await session.get(RegionModel, region_id)
    if not r:
        raise HTTPException(404, f"Region {region_id} not found")
    return r
