"""HxTest (#27) Phases D/E/F — AI test generation + app-bundled tests.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from case_service.hxtest import generator
from case_service.testsuite import dsl, runner


@pytest_asyncio.fixture
async def hxtest_installed(session):
    """Seed the standard marketplace install that enables HxTest for the test
    tenant. HxTest is gated by being installed (D2) — no bespoke flag — so the
    endpoint tests below exercise the installed path. The default admin token
    carries no tenant, so the gate resolves to tenant "default"."""
    from case_service.db.models import MarketplaceInstallModel
    session.add(MarketplaceInstallModel(
        tenant_id="default", package_id="velaris/hxtest",
        package_version="1.0.0", package_type="module", approved_by="test-admin"))
    await session.commit()
    yield


@pytest.mark.asyncio
async def test_endpoints_404_when_not_installed(client):
    """Gate: with no install row, HxTest is dark (D2 — dark until activation)."""
    r = await client.get("/api/v1/hxtest/generated")
    assert r.status_code == 404, r.text


# ── Phase D — structural generation (deterministic) ───────────────────────────

def test_generate_structural_shapes():
    defn = {"stages": [{"id": "s1", "name": "Open"}, {"id": "s2", "name": "Closed"}]}
    tests = generator.generate_structural("ct-123", defn)
    dsl.parse_suite(tests)                       # must be valid DSL
    asserts = tests[0]["steps"][0]["assert"]
    ops = {(a["op"], a.get("value")) for a in asserts}
    assert ("len", 2) in ops
    assert ("has", "Open") in ops and ("has", "Closed") in ops


@pytest.mark.asyncio
async def test_core_generate_no_hxtest_gate(session, client):
    """Core /testsuite/generate builds structural tests WITHOUT HxTest installed
    (deterministic core; the HxTest install gate must NOT apply here)."""
    r = await client.post("/api/v1/case-types", json={
        "name": "Core Gen CT", "version": "1.0.0",
        "lifecycle_process_id": str(uuid.uuid4()),
        "definition_json": {"stages": [{"id": "s1", "name": "Open"}, {"id": "s2", "name": "Closed"}]},
        "default_priority": "medium"})
    ct_id = r.json()["id"]
    g = await client.post("/api/v1/testsuite/generate", json={"case_type_id": ct_id})
    assert g.status_code == 200, g.text                  # NOT 404 — no HxTest gate
    assert g.json()["structural"] == 1
    assert g.json()["scenario_kept"] == 0


@pytest.mark.asyncio
async def test_generate_endpoint_then_run(session, client, hxtest_installed):
    # create a case type with stages
    r = await client.post("/api/v1/case-types", json={
        "name": "Gen CT", "version": "1.0.0",
        "lifecycle_process_id": str(uuid.uuid4()),
        "definition_json": {"stages": [{"id": "s1", "name": "Open"}, {"id": "s2", "name": "Closed"}]},
        "default_priority": "medium"})
    ct_id = r.json()["id"]
    # generate (structural only — AI likely unavailable in tests)
    g = await client.post("/api/v1/hxtest/generate",
                          json={"case_type_id": ct_id, "include_scenarios": False})
    assert g.status_code == 200, g.text
    assert g.json()["structural"] == 1
    # fetch the generated suite and run it through the core runner
    from case_service.db.models import TestSuiteModel
    from sqlalchemy import select
    suite = (await session.execute(
        select(TestSuiteModel).where(TestSuiteModel.case_type_id == uuid.UUID(ct_id)))).scalar_one()
    run = await runner.run_suite(session, suite.definition, suite_name="gen",
                                 triggered_by="t", clients={"admin": client})
    assert run.status == "passed", f"{run.status} f={run.failed}"


# ── Phase E — scenario generation (HxNexus; advisory) ─────────────────────────

@pytest.mark.asyncio
async def test_generate_scenarios_filters_invalid(monkeypatch):
    async def fake_generate_json(prompt, system="", **kw):
        return {"tests": [
            {"id": "good", "steps": [{"action": "api_get", "endpoint": "/health"}]},
            {"id": "bad", "steps": [{"action": "rm_rf", "endpoint": "/x"}]},  # invalid action
        ]}
    monkeypatch.setattr("case_service.hxnexus.factory.generate_json", fake_generate_json)
    out = await generator.generate_scenarios({"stages": []}, case_type_name="X")
    assert [t["id"] for t in out] == ["good"]
    assert out[0]["generated_by"] == "hxnexus"


@pytest.mark.asyncio
async def test_generate_scenarios_ai_unavailable(monkeypatch):
    async def none_generate_json(prompt, system="", **kw):
        return None
    monkeypatch.setattr("case_service.hxnexus.factory.generate_json", none_generate_json)
    assert await generator.generate_scenarios({"stages": []}) == []


# ── Phase F — app-bundled tests ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_bundled_rejects_bad_dsl(client, hxtest_installed):
    r = await client.post("/api/v1/hxtest/register-bundled", json={
        "name": "Acme bundled", "package_id": "acme",
        "tests": [{"id": "x", "steps": [{"action": "danger", "endpoint": "/x"}]}]})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_register_bundled_accepts_valid(client, hxtest_installed):
    r = await client.post("/api/v1/hxtest/register-bundled", json={
        "name": "Acme bundled", "package_id": "acme",
        "tests": [{"id": "ok", "steps": [{"action": "api_get", "endpoint": "/health",
                                          "assert": {"response_status": 200}}]}]})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
