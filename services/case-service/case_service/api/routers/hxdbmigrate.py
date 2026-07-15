"""HxDBMigrate API — P1 Connect + Discover · P2 Semantic/Compliance · P3 Case-Type
Generation · P4 Batch Migrate · P5 Continuous Sync · P6 Cutover/Rollback · P7
Compliance Migration Certificate.

Register + test read-only external source databases, run discovery, generate case
types, migrate + continuously sync rows into cases, cut over with a rollback
window, and produce the signed certificate. Admin-gated; tenant-scoped;
credentials HxVault-encrypted, decrypted only at connect. The source is never
written to.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import hxvault
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    CaseInstanceModel,
    HxDBMigrateAnalysisModel,
    HxDBMigrateMigrationRunModel,
    HxDBMigrateRowLinkModel,
    HxDBMigrateSourceModel,
)
from case_service.db.session import get_session
from case_service.db import repository as repo
from case_service.hxbridge.encryption import decrypt_credentials, encrypt_credentials
from case_service.hxdbmigrate import (casegen, certificate as cert_mod,
                                      migrate as migrate_mod, report,
                                      source as source_mod, sync as sync_mod)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/hxdbmigrate", tags=["hxdbmigrate"])


def _require_admin(user: AuthenticatedUser) -> None:
    roles = user.roles or []
    if not (user.has_privilege("*", "*") or "admin" in roles or "superadmin" in roles):
        raise HTTPException(403, "HxDBMigrate requires admin role")


def _tenant(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


def _case_type_visible(ct_tenant_id: Optional[str], user_tenant: str) -> bool:
    # Migration may only target a global case-type or one owned by the caller's tenant —
    # never another tenant's (cases inherit the case-type's tenant).
    return ct_tenant_id is None or ct_tenant_id == user_tenant


# ── schemas ───────────────────────────────────────────────────────────────────

class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_type: str          # postgresql | mysql | mariadb
    host: str
    port: Optional[int] = None
    database: str
    username: str
    password: str = ""
    ssl_mode: str = "disable"   # disable | require | verify


def _source_view(s: HxDBMigrateSourceModel) -> dict:
    # Never returns credentials.
    return {
        "id": str(s.id), "name": s.name, "source_type": s.source_type,
        "host": s.host, "port": s.port, "database": s.database, "username": s.username,
        "ssl_mode": s.ssl_mode,
        "status": s.status,
        "cutover_at": s.cutover_at.isoformat() if s.cutover_at else None,
        "rollback_window_hours": s.rollback_window_hours,
        "rollback_deadline": ((s.cutover_at + timedelta(hours=s.rollback_window_hours)).isoformat()
                              if s.cutover_at else None),
        "last_connected_at": s.last_connected_at.isoformat() if s.last_connected_at else None,
        "last_connect_ok": s.last_connect_ok,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _require_active(s: HxDBMigrateSourceModel) -> None:
    """Migrate/sync are frozen once a source is cut over (or completed)."""
    if s.status != "active":
        raise HTTPException(409, f"Source is '{s.status}' — data operations are "
                                 f"frozen after cutover (roll back to resume)")


async def _bg_graph_sync() -> None:
    """Re-sync HxGraph after a migration milestone (non-fatal, own connection)."""
    try:
        from sqlalchemy.ext.asyncio import AsyncSession as _S, async_sessionmaker
        from case_service.db.session import get_engine
        from case_service.hxgraph.sync import sync_graph
        async with async_sessionmaker(get_engine(), class_=_S,
                                      expire_on_commit=False)() as s:
            await sync_graph(s)
            await s.commit()
    except Exception:
        log.warning("hxdbmigrate: graph re-sync failed (non-fatal)", exc_info=True)


async def _get_source(session: AsyncSession, user: AuthenticatedUser, source_id: str) -> HxDBMigrateSourceModel:
    try:
        sid = uuid.UUID(source_id)
    except ValueError:
        raise HTTPException(404, "Source not found")
    s = (await session.execute(
        select(HxDBMigrateSourceModel).where(
            HxDBMigrateSourceModel.id == sid,
            HxDBMigrateSourceModel.tenant_id == _tenant(user),
        )
    )).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "Source not found")
    return s


# ── source-type allowlist (for the UI form) ─────────────────────────────────────

@router.get("/source-types")
async def get_source_types(user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    return {"source_types": source_mod.source_types(), "ssl_modes": source_mod.ssl_modes()}


# ── sources CRUD ────────────────────────────────────────────────────────────────

@router.post("/sources", status_code=201)
async def create_source(
    body: SourceCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    try:
        st = source_mod.normalise_type(body.source_type)
    except source_mod.SourceError as exc:
        raise HTTPException(400, str(exc))

    port = body.port or source_mod.default_port(st)
    # Validate host + credentials by actually connecting read-only before persisting.
    try:
        await source_mod.test_connection(st, body.host, port, body.database,
                                         body.username, body.password, body.ssl_mode)
    except source_mod.SourceError as exc:
        raise HTTPException(400, str(exc))

    existing = (await session.execute(
        select(HxDBMigrateSourceModel).where(
            HxDBMigrateSourceModel.name == body.name,
            HxDBMigrateSourceModel.tenant_id == _tenant(user),
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Source '{body.name}' already exists")

    await hxvault.ensure_dek(session, user.tenant_id)
    src = HxDBMigrateSourceModel(
        name=body.name, source_type=st, host=body.host, port=port,
        database=body.database, username=body.username, ssl_mode=body.ssl_mode,
        credentials=encrypt_credentials({"password": body.password}, tenant_id=user.tenant_id, vault=True),
        tenant_id=_tenant(user), created_by=user.user_id,
        last_connected_at=datetime.now(timezone.utc), last_connect_ok=True,
    )
    session.add(src)
    await session.commit()
    await session.refresh(src)
    return _source_view(src)


@router.get("/sources")
async def list_sources(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    rows = (await session.execute(
        select(HxDBMigrateSourceModel)
        .where(HxDBMigrateSourceModel.tenant_id == _tenant(user))
        .order_by(desc(HxDBMigrateSourceModel.created_at))
    )).scalars().all()
    return {"sources": [_source_view(s) for s in rows]}


@router.get("/sources/{source_id}")
async def get_source(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return _source_view(await _get_source(session, user, source_id))


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    await session.delete(s)
    await session.commit()


@router.post("/sources/{source_id}/test")
async def test_source(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    pw = decrypt_credentials(s.credentials).get("password", "")
    try:
        await source_mod.test_connection(s.source_type, s.host, s.port, s.database, s.username, pw, s.ssl_mode)
        ok = True
    except source_mod.SourceError as exc:
        ok = False
        detail = str(exc)
    s.last_connected_at = datetime.now(timezone.utc)
    s.last_connect_ok = ok
    await session.commit()
    if not ok:
        raise HTTPException(400, detail)
    return {"ok": True}


# ── discovery analysis ──────────────────────────────────────────────────────────

class AnalyzeOptions(BaseModel):
    deep: bool = True   # P2: sample values → semantic + compliance + mapping
    ai: bool = False    # optional AI narrative (egress-gated, local-only)


@router.post("/sources/{source_id}/analyze", status_code=201)
async def analyze_source(
    source_id: str,
    body: AnalyzeOptions = AnalyzeOptions(),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    pw = decrypt_credentials(s.credentials).get("password", "")

    analysis = HxDBMigrateAnalysisModel(
        source_id=s.id, tenant_id=_tenant(user), created_by=user.user_id,
    )
    try:
        async with source_mod.source_session(
            s.source_type, s.host, s.port, s.database, s.username, pw, s.ssl_mode
        ) as src_session:
            result = await report.analyze_source(src_session, deep=body.deep)
        if body.ai:
            from case_service.hxdbmigrate import ai_narrative
            result["ai_narrative"] = await ai_narrative.semantic_narrative(result)
        analysis.status = "complete"
        analysis.table_count = result["table_count"]
        analysis.quality_score = result["quality"]["score"]
        analysis.pii_count = result.get("pii_count")
        analysis.report = result
        s.last_connected_at = datetime.now(timezone.utc)
        s.last_connect_ok = True
    except source_mod.SourceError as exc:
        analysis.status = "failed"
        analysis.error = str(exc)
        s.last_connect_ok = False
    except Exception as exc:  # introspection failure — record, don't 500 silently
        analysis.status = "failed"
        analysis.error = f"Analysis failed: {exc}"

    session.add(analysis)
    await session.commit()
    await session.refresh(analysis)
    if analysis.status == "failed":
        raise HTTPException(400, analysis.error or "Analysis failed")
    return _analysis_view(analysis)


def _analysis_view(a: HxDBMigrateAnalysisModel) -> dict:
    return {
        "id": str(a.id), "source_id": str(a.source_id), "status": a.status,
        "table_count": a.table_count, "quality_score": a.quality_score,
        "pii_count": a.pii_count,
        "report": a.report, "error": a.error,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("/sources/{source_id}/analyses")
async def list_analyses(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    rows = (await session.execute(
        select(HxDBMigrateAnalysisModel)
        .where(HxDBMigrateAnalysisModel.source_id == s.id)
        .order_by(desc(HxDBMigrateAnalysisModel.created_at))
    )).scalars().all()
    # List view omits the full report body for brevity.
    return {"analyses": [
        {"id": str(a.id), "status": a.status, "table_count": a.table_count,
         "quality_score": a.quality_score, "pii_count": a.pii_count,
         "created_at": a.created_at.isoformat() if a.created_at else None}
        for a in rows
    ]}


# ── P3: case-type generation from schema ────────────────────────────────────────

async def _latest_deep_report(session: AsyncSession, source_id) -> dict | None:
    a = (await session.execute(
        select(HxDBMigrateAnalysisModel)
        .where(HxDBMigrateAnalysisModel.source_id == source_id,
               HxDBMigrateAnalysisModel.status == "complete")
        .order_by(desc(HxDBMigrateAnalysisModel.created_at))
    )).scalars().first()
    return a.report if (a and a.report and a.report.get("deep")) else None


@router.get("/sources/{source_id}/workflow-tables")
async def workflow_tables(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Candidate workflow tables (from the latest DEEP analysis — no new source read)."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    rep = await _latest_deep_report(session, s.id)
    if rep is None:
        return {"candidates": [], "hint": "Run a deep analysis first (Deep scan enabled)."}
    return {"candidates": casegen.detect_workflow_tables(rep.get("schema", []))}


class GenerateCaseTypeBody(BaseModel):
    table: str
    status_column: Optional[str] = None


@router.post("/sources/{source_id}/generate-case-type")
async def generate_case_type(
    source_id: str,
    body: GenerateCaseTypeBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Generate a DRAFT case-type definition_json from a source table (not applied)."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    pw = decrypt_credentials(s.credentials).get("password", "")
    try:
        async with source_mod.source_session(
            s.source_type, s.host, s.port, s.database, s.username, pw, s.ssl_mode
        ) as src_session:
            return await casegen.generate_case_type(src_session, body.table, body.status_column)
    except source_mod.SourceError as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class ApplyCaseTypeBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    version: str = "1.0.0"
    definition_json: dict
    description: str = ""


@router.post("/apply-case-type", status_code=201)
async def apply_case_type(
    body: ApplyCaseTypeBody,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a real (global) Velaris case-type from a reviewed draft, via the standard repo."""
    # Authorize exactly like the normal case-type create (privilege-based: case_type write
    # + admin.manage for a global type) — not merely the HxDBMigrate admin gate.
    from case_service.api.routers.case_types import _assert_can_write_case_type
    _assert_can_write_case_type(user, None, action="create")
    if await repo.get_case_type_by_name(session, body.name, body.version):
        raise HTTPException(409, f"Case type '{body.name}' v{body.version} already exists")
    ct = await repo.create_case_type(session, data={
        "name": body.name, "version": body.version, "tenant_id": None,
        "default_priority": "medium", "definition_json": body.definition_json,
        "description": body.description or f"Generated by HxDBMigrate from a migrated schema.",
        "tags": ["hxdbmigrate", "generated"],
    })
    await session.commit()
    background_tasks.add_task(_bg_graph_sync)   # new case type → knowledge graph
    return {"id": str(ct.id), "name": ct.name, "version": ct.version}


# ── P4: batch data migration (rows → cases) ─────────────────────────────────────

class MigrateBody(BaseModel):
    table: str
    case_type_id: str
    limit: int = 100
    offset: int = 0
    dry_run: bool = False
    pii_mode: str = "safe"   # safe | exclude_all | as_is


@router.post("/sources/{source_id}/migrate", status_code=201)
async def migrate_data(
    source_id: str,
    body: MigrateBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Batch-migrate source-table rows into Velaris cases of a target case-type."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    _require_active(s)

    try:
        ct_id = uuid.UUID(body.case_type_id)
    except ValueError:
        raise HTTPException(404, "case_type not found")
    ct = await repo.get_case_type(session, ct_id)
    if ct is None or not _case_type_visible(ct.tenant_id, _tenant(user)):
        raise HTTPException(404, "case_type not found")

    pw = decrypt_credentials(s.credentials).get("password", "")
    run = HxDBMigrateMigrationRunModel(
        source_id=s.id, tenant_id=_tenant(user), table_name=body.table,
        case_type_id=ct.id, pii_mode=body.pii_mode, dry_run=body.dry_run,
        created_by=user.user_id,
    )
    try:
        async with source_mod.source_session(
            s.source_type, s.host, s.port, s.database, s.username, pw, s.ssl_mode
        ) as src_session:
            result = await migrate_mod.migrate_table(
                src_session, session,
                table=body.table, case_type_id=ct.id, case_type_version=ct.version,
                tenant_id=ct.tenant_id, created_by=user.user_id,
                limit=body.limit, offset=body.offset,
                pii_mode=body.pii_mode, dry_run=body.dry_run,
                source_id=s.id,      # P5: writes row links + skips already-linked
            )
        run.status = "dry_run" if body.dry_run else "complete"
        run.rows_read = result["rows_read"]
        run.rows_migrated = result["rows_migrated"]
        run.excluded_columns = result["excluded_columns"]
    except (source_mod.SourceError, ValueError) as exc:
        # Discard any partially-created cases so a failed run leaves nothing behind.
        await session.rollback()
        run.status = "failed"
        run.error = str(exc)
        session.add(run)
        await session.commit()
        raise HTTPException(400, str(exc))

    session.add(run)
    await session.commit()
    await session.refresh(run)
    return {"run_id": str(run.id), "status": run.status, **result}


# ── P5: continuous sync (polling Migration Twin) ────────────────────────────────

class SyncBody(BaseModel):
    table: str
    case_type_id: str
    pii_mode: str = "safe"
    after_pk: Optional[str] = None    # continuation cursor from a previous pass


@router.post("/sources/{source_id}/sync", status_code=201)
async def sync_source_table(
    source_id: str,
    body: SyncBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """One bounded incremental sync pass: new source rows → cases, changed rows →
    case-data updates (idempotent via row links)."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    _require_active(s)

    try:
        ct_id = uuid.UUID(body.case_type_id)
    except ValueError:
        raise HTTPException(404, "case_type not found")
    ct = await repo.get_case_type(session, ct_id)
    if ct is None or not _case_type_visible(ct.tenant_id, _tenant(user)):
        raise HTTPException(404, "case_type not found")

    pw = decrypt_credentials(s.credentials).get("password", "")
    run = HxDBMigrateMigrationRunModel(
        source_id=s.id, tenant_id=_tenant(user), table_name=body.table,
        case_type_id=ct.id, kind="sync", pii_mode=body.pii_mode,
        created_by=user.user_id,
    )
    try:
        async with source_mod.source_session(
            s.source_type, s.host, s.port, s.database, s.username, pw, s.ssl_mode
        ) as src_session:
            result = await sync_mod.sync_table(
                src_session, session,
                source_id=s.id, table=body.table,
                case_type_id=ct.id, case_type_version=ct.version,
                tenant_id=ct.tenant_id, created_by=user.user_id,
                pii_mode=body.pii_mode, after_pk=body.after_pk,
            )
        run.status = "complete"
        run.rows_read = result["rows_read"]
        run.rows_migrated = result["cases_created"]
        run.rows_updated = result["cases_updated"]
    except (source_mod.SourceError, sync_mod.SyncError, ValueError) as exc:
        # Discard any partially-written cases/links so a failed pass leaves nothing.
        await session.rollback()
        run.status = "failed"
        run.error = str(exc)
        session.add(run)
        await session.commit()
        raise HTTPException(400, str(exc))

    session.add(run)
    await session.commit()
    await session.refresh(run)
    return {"run_id": str(run.id), "status": run.status, **result}


@router.get("/sources/{source_id}/sync-status")
async def get_sync_status(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Per-table coverage vs the LIVE source + the Migration Health Score."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    pw = decrypt_credentials(s.credentials).get("password", "")
    try:
        async with source_mod.source_session(
            s.source_type, s.host, s.port, s.database, s.username, pw, s.ssl_mode
        ) as src_session:
            status = await sync_mod.sync_status(src_session, session, source_id=s.id)
    except source_mod.SourceError as exc:
        raise HTTPException(400, str(exc))
    return {**status, "source_status": s.status}


# ── P6: one-click cutover + rollback window ─────────────────────────────────────

_CUTOVER_MAX_PASSES_PER_TABLE = 20   # × sync page bounds → hard cap per table


class CutoverBody(BaseModel):
    rollback_window_hours: Optional[int] = Field(default=None, ge=1, le=24 * 30)


@router.post("/sources/{source_id}/cutover")
async def cutover_source(
    source_id: str,
    background_tasks: BackgroundTasks,
    body: CutoverBody = CutoverBody(),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """One click: final delta sync on every linked table, then freeze the source.

    From here Velaris is the system of record; the source was never written to,
    so rollback (within the window) is simply deleting the Velaris copy."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    _require_active(s)

    # distinct (table, case-type) pairs this source has migrated
    pairs = (await session.execute(
        select(HxDBMigrateRowLinkModel.table_name,
               HxDBMigrateRowLinkModel.case_type_id)
        .where(HxDBMigrateRowLinkModel.source_id == s.id)
        .distinct()
    )).all()
    if not pairs:
        raise HTTPException(400, "Nothing to cut over — no linked migrations exist "
                                 "for this source (run a migration first)")

    pw = decrypt_credentials(s.credentials).get("password", "")
    totals = {"rows_read": 0, "cases_created": 0, "cases_updated": 0}
    synced_tables: list[str] = []
    try:
        async with source_mod.source_session(
            s.source_type, s.host, s.port, s.database, s.username, pw, s.ssl_mode
        ) as src_session:
            for table_name, ct_id in pairs:
                ct = await repo.get_case_type(session, ct_id) if ct_id else None
                if ct is None:
                    continue           # case-type deleted since — nothing to sync into
                cursor = None
                for _ in range(_CUTOVER_MAX_PASSES_PER_TABLE):
                    result = await sync_mod.sync_table(
                        src_session, session,
                        source_id=s.id, table=table_name,
                        case_type_id=ct.id, case_type_version=ct.version,
                        tenant_id=ct.tenant_id, created_by=user.user_id,
                        pii_mode="safe", after_pk=cursor,
                    )
                    for key in totals:
                        totals[key] += result[key]
                    cursor = result["next_after_pk"]
                    if result["done"]:
                        break
                synced_tables.append(table_name)
    except (source_mod.SourceError, sync_mod.SyncError) as exc:
        await session.rollback()
        raise HTTPException(400, f"Final delta sync failed — cutover aborted, "
                                 f"source stays active: {exc}")

    now = datetime.now(timezone.utc)
    s.status = "cutover"
    s.cutover_at = now
    if body.rollback_window_hours:
        s.rollback_window_hours = body.rollback_window_hours
    session.add(HxDBMigrateMigrationRunModel(
        source_id=s.id, tenant_id=_tenant(user), table_name=",".join(synced_tables)[:255],
        kind="cutover", status="complete",
        rows_read=totals["rows_read"], rows_migrated=totals["cases_created"],
        rows_updated=totals["cases_updated"], created_by=user.user_id,
    ))
    await session.commit()
    background_tasks.add_task(_bg_graph_sync)   # migrated schema → knowledge graph
    return {**_source_view(s), "final_sync": totals, "tables": synced_tables}


@router.post("/sources/{source_id}/rollback")
async def rollback_source(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Inside the rollback window: cancel every case this source created (soft —
    status='cancelled', auditable), drop the links, unfreeze the source."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    if s.status != "cutover":
        raise HTTPException(400, f"Only a cut-over source can be rolled back "
                                 f"(current: '{s.status}')")
    cutover_at = (s.cutover_at.replace(tzinfo=timezone.utc)
                  if s.cutover_at.tzinfo is None else s.cutover_at)
    deadline = cutover_at + timedelta(hours=s.rollback_window_hours)
    if datetime.now(timezone.utc) > deadline:
        raise HTTPException(409, f"Rollback window expired at {deadline.isoformat()} "
                                 f"— the migration is final")

    links = (await session.execute(
        select(HxDBMigrateRowLinkModel)
        .where(HxDBMigrateRowLinkModel.source_id == s.id)
    )).scalars().all()
    cancelled = 0
    for link in links:
        case = await session.get(CaseInstanceModel, link.case_id)
        if case is not None and case.status != "cancelled":
            case.status = "cancelled"
            cancelled += 1
        await session.delete(link)

    s.status = "active"
    s.cutover_at = None
    session.add(HxDBMigrateMigrationRunModel(
        source_id=s.id, tenant_id=_tenant(user), table_name="*",
        kind="rollback", status="complete", rows_migrated=cancelled,
        created_by=user.user_id,
    ))
    await session.commit()
    return {**_source_view(s), "cases_cancelled": cancelled,
            "links_removed": len(links)}


@router.post("/sources/{source_id}/complete")
async def complete_source(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Mark a cut-over migration as final (closes the rollback window early)."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    if s.status != "cutover":
        raise HTTPException(400, f"Only a cut-over source can be completed "
                                 f"(current: '{s.status}')")
    s.status = "completed"
    await session.commit()
    return _source_view(s)


# ── P7: Compliance Migration Certificate ────────────────────────────────────────

@router.get("/sources/{source_id}/certificate")
async def get_certificate(
    source_id: str,
    fmt: str = "json",
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The signed Compliance Migration Certificate (fmt=json | pdf)."""
    _require_admin(user)
    s = await _get_source(session, user, source_id)
    signed = await cert_mod.build_certificate(session, s)
    if fmt == "pdf":
        pdf = cert_mod.render_pdf(signed)
        return Response(content=pdf, media_type="application/pdf", headers={
            "Content-Disposition":
                f'attachment; filename="migration-certificate-{s.name}.pdf"'})
    return signed


@router.get("/analyses/{analysis_id}")
async def get_analysis(
    analysis_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    try:
        aid = uuid.UUID(analysis_id)
    except ValueError:
        raise HTTPException(404, "Analysis not found")
    a = (await session.execute(
        select(HxDBMigrateAnalysisModel).where(
            HxDBMigrateAnalysisModel.id == aid,
            HxDBMigrateAnalysisModel.tenant_id == _tenant(user),
        )
    )).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Analysis not found")
    return _analysis_view(a)
