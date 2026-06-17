"""P29 HxSync — Data Pipeline & Warehouse Bridge API router."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    SyncDestinationModel,
    SyncFieldMappingModel,
    SyncRedactionRuleModel,
    SyncRunModel,
)
from case_service.db.session import get_analytics_session as get_session
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.hxsync import destinations as _dest_mod  # noqa: F401
from case_service.hxsync.protocol import get_destination
from case_service.hxsync.pipeline import run_sync

router = APIRouter(prefix="/sync", tags=["hxsync"])

DEST_TYPES = ["duckdb", "bigquery", "snowflake", "redshift", "kafka", "kinesis", "pubsub"]


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CreateDestinationRequest(BaseModel):
    name: str
    dest_type: str = "duckdb"
    connection_config: dict = {}
    enabled: bool = True

class UpdateDestinationRequest(BaseModel):
    name: Optional[str] = None
    connection_config: Optional[dict] = None
    enabled: Optional[bool] = None

class CreateFieldMappingRequest(BaseModel):
    case_type_id: Optional[str] = None
    source_field: str
    dest_column: str
    transform: str = "passthrough"
    pii: bool = False

class CreateRedactionRuleRequest(BaseModel):
    case_type_id: Optional[str] = None
    field_path: str
    action: str = "hash"
    reason: Optional[str] = None


# ── Serialisers ───────────────────────────────────────────────────────────────

def _dest(d: SyncDestinationModel) -> dict:
    return {
        "id": str(d.id), "name": d.name, "dest_type": d.dest_type,
        "connection_config": d.connection_config, "enabled": d.enabled,
        "tenant_id": d.tenant_id, "created_by": d.created_by,
        "last_synced_at": d.last_synced_at.isoformat() if d.last_synced_at else None,
        "last_sync_status": d.last_sync_status,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }

def _run(r: SyncRunModel) -> dict:
    return {
        "id": str(r.id), "destination_id": str(r.destination_id),
        "status": r.status, "rows_synced": r.rows_synced, "error_msg": r.error_msg,
        "watermark_from": r.watermark_from.isoformat() if r.watermark_from else None,
        "watermark_to":   r.watermark_to.isoformat()   if r.watermark_to   else None,
        "started_at":     r.started_at.isoformat()     if r.started_at     else None,
        "finished_at":    r.finished_at.isoformat()    if r.finished_at    else None,
    }

def _mapping(m: SyncFieldMappingModel) -> dict:
    return {
        "id": str(m.id), "destination_id": str(m.destination_id),
        "case_type_id": m.case_type_id, "source_field": m.source_field,
        "dest_column": m.dest_column, "transform": m.transform, "pii": m.pii,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }

def _redaction(r: SyncRedactionRuleModel) -> dict:
    return {
        "id": str(r.id), "destination_id": str(r.destination_id),
        "case_type_id": r.case_type_id, "field_path": r.field_path,
        "action": r.action, "reason": r.reason,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ── Destinations ──────────────────────────────────────────────────────────────

@router.get("/destinations")
async def list_destinations(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    rows = (await session.execute(
        select(SyncDestinationModel).order_by(SyncDestinationModel.created_at)
    )).scalars().all()
    return {"destinations": [_dest(d) for d in rows], "total": len(rows)}


@router.post("/destinations", status_code=201)
async def create_destination(
    body: CreateDestinationRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if body.dest_type not in DEST_TYPES:
        raise HTTPException(400, f"Invalid dest_type. Must be one of: {DEST_TYPES}")
    d = SyncDestinationModel(
        name=body.name, dest_type=body.dest_type,
        connection_config=body.connection_config, enabled=body.enabled,
        created_by=user.user_id,
    )
    session.add(d)
    await session.commit()
    await session.refresh(d)
    return _dest(d)


@router.get("/destinations/{dest_id}")
async def get_destination_endpoint(
    dest_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    return _dest(await _dest_or_404(session, dest_id))


@router.patch("/destinations/{dest_id}")
async def update_destination(
    dest_id: uuid.UUID,
    body: UpdateDestinationRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    d = await _dest_or_404(session, dest_id)
    if body.name is not None:
        d.name = body.name
    if body.connection_config is not None:
        d.connection_config = body.connection_config
    if body.enabled is not None:
        d.enabled = body.enabled
    d.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(d)
    return _dest(d)


@router.delete("/destinations/{dest_id}", status_code=204)
async def delete_destination(
    dest_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    d = await _dest_or_404(session, dest_id)
    await session.delete(d)
    await session.commit()


@router.post("/destinations/{dest_id}/test")
async def test_destination(
    dest_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    d = await _dest_or_404(session, dest_id)
    adapter = get_destination(d.dest_type, d.connection_config)
    result = adapter.health_check()
    return result


# ── Sync execution ────────────────────────────────────────────────────────────

@router.post("/run/{dest_id}", status_code=202)
async def trigger_sync(
    dest_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Trigger an async sync run. Returns immediately with run_id."""
    await _dest_or_404(session, dest_id)

    async def _run_bg():
        from case_service.db.session import get_analytics_session_factory
        async with get_analytics_session_factory()() as bg_session:
            await run_sync(dest_id, bg_session)

    background_tasks.add_task(_run_bg)
    return {"status": "queued", "destination_id": str(dest_id)}


@router.post("/run/{dest_id}/sync")
async def trigger_sync_blocking(
    dest_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Synchronous sync run — waits for completion. Good for small datasets or testing."""
    await _dest_or_404(session, dest_id)
    return await run_sync(dest_id, session)


# ── Run history ───────────────────────────────────────────────────────────────

@router.get("/runs")
async def list_runs(
    destination_id: Optional[uuid.UUID] = None,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    q = select(SyncRunModel).order_by(desc(SyncRunModel.started_at))
    if destination_id:
        q = q.where(SyncRunModel.destination_id == destination_id)
    rows = (await session.execute(q)).scalars().all()
    return {"runs": [_run(r) for r in rows], "total": len(rows)}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await session.get(SyncRunModel, run_id)
    if not r:
        raise HTTPException(404, f"Run {run_id} not found")
    return _run(r)


# ── Health summary ────────────────────────────────────────────────────────────

@router.get("/health")
async def sync_health(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    dests = (await session.execute(select(SyncDestinationModel))).scalars().all()
    items = []
    for d in dests:
        try:
            adapter = get_destination(d.dest_type, d.connection_config)
            health = adapter.health_check()
        except Exception as exc:
            health = {"ok": False, "message": str(exc), "latency_ms": 0}
        items.append({
            "id": str(d.id), "name": d.name, "dest_type": d.dest_type,
            "enabled": d.enabled, "last_synced_at": d.last_synced_at.isoformat() if d.last_synced_at else None,
            "last_sync_status": d.last_sync_status,
            **health,
        })
    return {"health": items, "total": len(items)}


# ── Field mappings ────────────────────────────────────────────────────────────

@router.get("/destinations/{dest_id}/field-mappings")
async def list_field_mappings(
    dest_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    await _dest_or_404(session, dest_id)
    rows = (await session.execute(
        select(SyncFieldMappingModel).where(SyncFieldMappingModel.destination_id == dest_id)
    )).scalars().all()
    return {"mappings": [_mapping(m) for m in rows], "total": len(rows)}


@router.post("/destinations/{dest_id}/field-mappings", status_code=201)
async def create_field_mapping(
    dest_id: uuid.UUID,
    body: CreateFieldMappingRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    await _dest_or_404(session, dest_id)
    m = SyncFieldMappingModel(
        destination_id=dest_id,
        case_type_id=body.case_type_id,
        source_field=body.source_field,
        dest_column=body.dest_column,
        transform=body.transform,
        pii=body.pii,
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)
    return _mapping(m)


@router.delete("/field-mappings/{mapping_id}", status_code=204)
async def delete_field_mapping(
    mapping_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    m = await session.get(SyncFieldMappingModel, mapping_id)
    if not m:
        raise HTTPException(404, "Mapping not found")
    await session.delete(m)
    await session.commit()


# ── Redaction rules ───────────────────────────────────────────────────────────

@router.get("/destinations/{dest_id}/redaction-rules")
async def list_redaction_rules(
    dest_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    await _dest_or_404(session, dest_id)
    rows = (await session.execute(
        select(SyncRedactionRuleModel).where(SyncRedactionRuleModel.destination_id == dest_id)
    )).scalars().all()
    return {"rules": [_redaction(r) for r in rows], "total": len(rows)}


@router.post("/destinations/{dest_id}/redaction-rules", status_code=201)
async def create_redaction_rule(
    dest_id: uuid.UUID,
    body: CreateRedactionRuleRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    await _dest_or_404(session, dest_id)
    if body.action not in ("hash", "drop", "mask"):
        raise HTTPException(400, "action must be hash, drop, or mask")
    r = SyncRedactionRuleModel(
        destination_id=dest_id,
        case_type_id=body.case_type_id,
        field_path=body.field_path,
        action=body.action,
        reason=body.reason,
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return _redaction(r)


@router.delete("/redaction-rules/{rule_id}", status_code=204)
async def delete_redaction_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await session.get(SyncRedactionRuleModel, rule_id)
    if not r:
        raise HTTPException(404, "Redaction rule not found")
    await session.delete(r)
    await session.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _dest_or_404(session: AsyncSession, dest_id: uuid.UUID) -> SyncDestinationModel:
    d = await session.get(SyncDestinationModel, dest_id)
    if not d:
        raise HTTPException(404, f"Destination {dest_id} not found")
    return d
