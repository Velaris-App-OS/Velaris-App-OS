"""Test Suite (#27) Phase A — runner persistence + isolation roundtrip.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from case_service.db.models import TestResultModel, TenantModel
from case_service.testsuite import runner, isolation, builtin, dsl


@pytest.mark.asyncio
async def test_run_suite_persists(session, client, anon_client):
    clients = {"admin": client, "none": anon_client}
    suite = [
        {"id": "h", "name": "health", "steps": [
            {"action": "api_get", "endpoint": "/health", "assert": {"response_status": 200}}]},
        {"id": "a", "name": "anon rejected", "steps": [
            {"action": "api_get", "endpoint": "/api/v1/case-types", "identity": "none",
             "assert": {"response_status": 401}}]},
    ]
    run = await runner.run_suite(session, suite, suite_name="t", triggered_by="tester", clients=clients)
    assert run.status == "passed"
    assert (run.total, run.passed, run.failed) == (2, 2, 0)
    results = (await session.execute(
        select(TestResultModel).where(TestResultModel.run_id == run.id))).scalars().all()
    assert len(results) == 2
    assert all(r.status == "passed" for r in results)


@pytest.mark.asyncio
async def test_run_suite_partial(session, client, anon_client):
    clients = {"admin": client, "none": anon_client}
    suite = [
        {"id": "ok", "steps": [{"action": "api_get", "endpoint": "/health",
                                "assert": {"response_status": 200}}]},
        {"id": "bad", "steps": [{"action": "api_get", "endpoint": "/health",
                                 "assert": {"response_status": 500}}]},
    ]
    run = await runner.run_suite(session, suite, suite_name="t", triggered_by="tester", clients=clients)
    assert run.status == "partial"
    assert (run.passed, run.failed) == (1, 1)


def test_builtin_smoke_parses():
    # the built-in suite must always be DSL-valid
    dsl.parse_suite(builtin.get_builtin_suite("platform-smoke"))
    assert {s["name"] for s in builtin.list_builtin_suites()} >= {"platform-smoke", "component", "security"}


@pytest.mark.asyncio
async def test_ephemeral_tenant_roundtrip(session):
    run_id = uuid.uuid4()
    tid = await isolation.provision_ephemeral_tenant(session, run_id, "a")
    row = await session.get(TenantModel, tid)
    assert row is not None and row.slug.startswith("hxtest-")
    await isolation.teardown_ephemeral_tenant(session, tid)
    assert await session.get(TenantModel, tid) is None
