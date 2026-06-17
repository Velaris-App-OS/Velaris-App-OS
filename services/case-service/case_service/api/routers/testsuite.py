"""Test Suite (#27) — core runner API. Admin-gated (it mints tokens, D5).

POST /api/v1/testsuite/run         run a suite (optionally isolated)
GET  /api/v1/testsuite/runs        list runs
GET  /api/v1/testsuite/runs/{id}   run detail + per-test results
GET  /api/v1/testsuite/runs/{id}/stream   SSE live results
GET  /api/v1/testsuite/suites      registered/built-in suites

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_admin
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    TestSuiteModel, TestRunModel, TestResultModel, MarketplaceWorkspaceModel, CaseTypeModel,
)
from case_service.db.session import get_session
from case_service.testsuite import isolation, runner, conformance
from case_service.testsuite.builtin import get_builtin_suite, list_builtin_suites
from case_service.testsuite.generator import generate_structural

router = APIRouter(prefix="/testsuite", tags=["testsuite"])


class RunReq(BaseModel):
    suite: str                       # builtin name or suite uuid
    app_package_id: str | None = None
    isolate: bool = False            # provision disposable tenants for mutating runs


def _build_clients(app, tenant_a: uuid.UUID | None, tenant_b: uuid.UUID | None) -> dict:
    """Per-identity in-process ASGI clients (decision D5). No network hop."""
    headers = isolation.identity_headers(tenant_a, tenant_b)
    transport = ASGITransport(app=app)
    return {
        ident: AsyncClient(transport=transport, base_url="http://testsuite", headers=h)
        for ident, h in headers.items()
    }


async def _resolve_suite(session: AsyncSession, name: str) -> tuple[list, str, uuid.UUID | None]:
    """Return (definition, display_name, suite_id) for a builtin name or a stored uuid."""
    builtin = get_builtin_suite(name)
    if builtin is not None:
        return builtin, name, None
    try:
        sid = uuid.UUID(name)
    except ValueError:
        raise HTTPException(404, f"Unknown suite '{name}'")
    row = await session.get(TestSuiteModel, sid)
    if row is None:
        raise HTTPException(404, f"Suite {sid} not found")
    return row.definition, row.name, row.id


@router.post("/run")
async def run(
    body: RunReq,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    from case_service.main import app  # late import avoids cycle
    definition, name, suite_id = await _resolve_suite(session, body.suite)

    tenant_a = tenant_b = None
    eph: list[uuid.UUID] = []
    if body.isolate:
        run_uuid = uuid.uuid4()
        tenant_a = await isolation.provision_ephemeral_tenant(session, run_uuid, "a")
        tenant_b = await isolation.provision_ephemeral_tenant(session, run_uuid, "b")
        eph = [tenant_a, tenant_b]
        await session.commit()

    clients = _build_clients(app, tenant_a, tenant_b)
    try:
        run_row = await runner.run_suite(
            session, definition, suite_name=name, suite_id=suite_id,
            triggered_by=user.user_id,
            app_package_id=uuid.UUID(body.app_package_id) if body.app_package_id else None,
            clients=clients,
            ephemeral_tenant_id=str(tenant_a) if tenant_a else None,
        )
        await session.commit()
    finally:
        for c in clients.values():
            await c.aclose()
        for tid in eph:                    # guaranteed teardown (D1)
            await isolation.teardown_ephemeral_tenant(session, tid)
        await session.commit()

    return {"run_id": str(run_row.id), "status": run_row.status,
            "total": run_row.total, "passed": run_row.passed,
            "failed": run_row.failed, "skipped": run_row.skipped}


class ConformanceReq(BaseModel):
    package: dict                    # {manifest, case_types[], forms[], rules[]}
    workspace_id: str | None = None  # attach the result to a marketplace workspace


@router.post("/conformance")
async def run_conformance(
    body: ConformanceReq,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run the structural Conformance Suite against a package (the HARD gate, D3)."""
    from datetime import datetime, timezone
    run = await conformance.record_conformance_run(
        session, body.package, triggered_by=user.user_id)
    if body.workspace_id:
        ws = await session.get(MarketplaceWorkspaceModel, uuid.UUID(body.workspace_id))
        if ws is not None:
            ws.conformance_status = "structural_passed" if run.status == "passed" else "unverified"
            ws.conformance_run_id = run.id
            ws.conformance_checked_at = datetime.now(timezone.utc)
            await session.commit()
    detail = await session.get(TestRunModel, run.id)
    return {"run_id": str(run.id), "passed": run.status == "passed",
            "total": detail.total, "failed": detail.failed}


class GenerateReq(BaseModel):
    case_type_id: str


@router.post("/generate")
async def generate_suite(
    body: GenerateReq,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate the deterministic STRUCTURAL test suite for a case type.

    Core Test Suite — no LLM, no HxTest install required. Any AI scenarios already
    attached to the case type's generated suite (added via /hxtest/generate) are
    preserved; only the structural test(s) are (re)built here."""
    ct = await session.get(CaseTypeModel, uuid.UUID(body.case_type_id))
    if ct is None:
        raise HTTPException(404, "Case type not found")

    structural = generate_structural(str(ct.id), ct.definition_json or {})
    existing = (await session.execute(
        select(TestSuiteModel).where(
            TestSuiteModel.case_type_id == ct.id, TestSuiteModel.suite_type == "generated")
    )).scalar_one_or_none()
    if existing is not None:
        ai_tests = [t for t in (existing.definition or [])
                    if t.get("generated_by") not in (None, "structural")]
        existing.definition = structural + ai_tests
        existing.version = ct.version
        if ai_tests:
            existing.ai_stale = True          # structural refreshed → AI now stale
        suite_id, n_ai = existing.id, len(ai_tests)
    else:
        suite = TestSuiteModel(name=f"Generated · {ct.name}", suite_type="generated",
                               source="structural", case_type_id=ct.id,
                               definition=structural, version=ct.version)
        session.add(suite)
        await session.flush()
        suite_id, n_ai = suite.id, 0
    await session.commit()
    return {"suite_id": str(suite_id), "structural": len(structural),
            "scenario_kept": n_ai, "total": len(structural) + n_ai}


@router.get("/runs")
async def list_runs(
    _admin=Depends(require_admin()),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(TestRunModel).order_by(TestRunModel.started_at.desc()).limit(100)
    )).scalars().all()
    return [{"id": str(r.id), "suite_name": r.suite_name, "status": r.status,
             "total": r.total, "passed": r.passed, "failed": r.failed,
             "skipped": r.skipped, "started_at": r.started_at.isoformat()} for r in rows]


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: uuid.UUID,
    _admin=Depends(require_admin()),
    session: AsyncSession = Depends(get_session),
):
    run_row = await session.get(TestRunModel, run_id)
    if run_row is None:
        raise HTTPException(404, "Run not found")
    results = (await session.execute(
        select(TestResultModel).where(TestResultModel.run_id == run_id)
    )).scalars().all()
    return {
        "id": str(run_row.id), "suite_name": run_row.suite_name, "status": run_row.status,
        "total": run_row.total, "passed": run_row.passed, "failed": run_row.failed,
        "skipped": run_row.skipped,
        "results": [{"test_id": r.test_id, "test_name": r.test_name, "status": r.status,
                     "duration_ms": r.duration_ms, "error_detail": r.error_detail,
                     "step_results": r.step_results} for r in results],
    }


@router.get("/runs/{run_id}/stream")
async def run_stream(
    run_id: uuid.UUID,
    _admin=Depends(require_admin()),
    session: AsyncSession = Depends(get_session),
):
    """SSE: replay a completed run's results as events (live streaming during a
    run is a Phase-B refinement; this serves persisted results deterministically)."""
    run_row = await session.get(TestRunModel, run_id)
    if run_row is None:
        raise HTTPException(404, "Run not found")
    results = (await session.execute(
        select(TestResultModel).where(TestResultModel.run_id == run_id)
    )).scalars().all()

    async def gen():
        yield f"data: {json.dumps({'event': 'run_started', 'total': run_row.total})}\n\n"
        for r in results:
            yield f"data: {json.dumps({'event': 'test_result', 'test_id': r.test_id, 'status': r.status})}\n\n"
        yield f"data: {json.dumps({'event': 'run_complete', 'status': run_row.status})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/suites")
async def list_suites(
    _user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    builtin = list_builtin_suites()
    stored = (await session.execute(select(TestSuiteModel))).scalars().all()
    return {
        "builtin": builtin,
        "stored": [{"id": str(s.id), "name": s.name, "suite_type": s.suite_type,
                    "source": s.source, "version": s.version,
                    "case_type_id": str(s.case_type_id) if s.case_type_id else None,
                    "ai_stale": s.ai_stale,
                    "count": len(s.definition or [])} for s in stored],
    }
