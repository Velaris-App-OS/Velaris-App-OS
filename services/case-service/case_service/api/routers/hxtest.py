"""HxTest (#27) — marketplace AI layer API. Built on the core Test Suite.

POST /api/v1/hxtest/generate          generate structural (+ scenario) tests for a case type
GET  /api/v1/hxtest/generated         list generated suites
POST /api/v1/hxtest/register-bundled  register an app-bundled test suite (Phase F)

Enablement is the STANDARD marketplace-install mechanism (decision D2), not a
bespoke flag: HxTest is a `module`-type `.hxapp` (package id `velaris/hxtest`),
"enabled on install" like every other marketplace module. Availability == the
package is installed (non-revoked) for the caller's tenant. Official vs
third-party differs only by trust tier (which org the source URL belongs to),
never by a separate on/off switch. The Python code ships in-image (the trust
model forbids loading in-process code from a package); install simply mounts
these routes per tenant via the install record.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_admin
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import TestSuiteModel, CaseTypeModel, MarketplaceInstallModel
from case_service.db.session import get_session
from case_service.hxtest import generator
from case_service.testsuite import dsl

router = APIRouter(prefix="/hxtest", tags=["hxtest"])

# The marketplace package whose install enables HxTest. Must match the `id` in
# the published velaris.json manifest and the gate query below — a mismatch means
# HxTest never enables on install.
HXTEST_PACKAGE_ID = "velaris/hxtest"


async def _require_enabled(session: AsyncSession, user: AuthenticatedUser) -> None:
    """D2 gate: HxTest is available iff its marketplace package is installed.

    No bespoke flag — this is the universal "feature flag enabled on install"
    mechanism every `module` package uses. The `marketplace_installs` table only
    exists once the marketplace itself is activated; until then (and whenever the
    package isn't installed for this tenant) HxTest 404s. The existence check uses
    a SAVEPOINT so a missing table can't poison the request transaction, and stays
    DB-agnostic (no Postgres-only `to_regclass`)."""
    tenant = user.tenant_id or "default"
    try:
        async with session.begin_nested():
            row = (await session.execute(
                select(MarketplaceInstallModel.id).where(
                    MarketplaceInstallModel.tenant_id == tenant,
                    MarketplaceInstallModel.package_id == HXTEST_PACKAGE_ID,
                    MarketplaceInstallModel.revoked_at.is_(None),
                ).limit(1))).first()
    except (ProgrammingError, OperationalError):
        row = None                       # marketplace not yet activated → table absent
    if row is None:
        raise HTTPException(404, "HxTest is not installed on this instance")


class GenerateReq(BaseModel):
    case_type_id: str
    include_scenarios: bool = True   # Layer 2 (HxNexus); skipped if AI unavailable


@router.post("/generate")
async def generate(
    body: GenerateReq,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _require_enabled(session, user)
    ct = await session.get(CaseTypeModel, uuid.UUID(body.case_type_id))
    if ct is None:
        raise HTTPException(404, "Case type not found")

    tests = generator.generate_structural(str(ct.id), ct.definition_json or {})
    n_scenario = 0
    if body.include_scenarios:
        scenarios = await generator.generate_scenarios(ct.definition_json or {}, case_type_name=ct.name)
        tests += scenarios
        n_scenario = len(scenarios)

    # Replace any prior generated suite for this case type.
    existing = (await session.execute(
        select(TestSuiteModel).where(
            TestSuiteModel.case_type_id == ct.id, TestSuiteModel.suite_type == "generated")
    )).scalar_one_or_none()
    if existing is not None:
        existing.definition = tests
        existing.version = ct.version
        existing.ai_stale = False            # AI scenarios just regenerated → fresh
        suite_id = existing.id
    else:
        suite = TestSuiteModel(name=f"Generated · {ct.name}", suite_type="generated",
                               source="ai_generated", case_type_id=ct.id,
                               definition=tests, version=ct.version, ai_stale=False)
        session.add(suite)
        await session.flush()
        suite_id = suite.id
    await session.commit()
    return {"suite_id": str(suite_id), "total": len(tests),
            "structural": len(tests) - n_scenario, "scenario": n_scenario}


@router.get("/generated")
async def list_generated(
    _user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _require_enabled(session, _user)
    rows = (await session.execute(
        select(TestSuiteModel).where(TestSuiteModel.suite_type == "generated"))).scalars().all()
    return [{"id": str(s.id), "name": s.name, "case_type_id": str(s.case_type_id) if s.case_type_id else None,
             "version": s.version, "ai_stale": s.ai_stale, "count": len(s.definition or [])} for s in rows]


class RegisterBundledReq(BaseModel):
    name: str
    package_id: str
    tests: list                      # DSL test defs bundled with the app


@router.post("/register-bundled")
async def register_bundled(
    body: RegisterBundledReq,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Phase F: register a marketplace app's bundled test suite (closed-DSL only)."""
    await _require_enabled(session, user)
    try:
        dsl.parse_suite(body.tests)              # reject anything outside the closed DSL (D4)
    except dsl.DslError as e:
        raise HTTPException(400, f"Bundled tests rejected: {e}")
    suite = TestSuiteModel(name=body.name, suite_type="component", source="developer",
                           definition=body.tests, version="1.0.0")
    session.add(suite)
    await session.commit()
    return {"suite_id": str(suite.id), "count": len(body.tests)}
