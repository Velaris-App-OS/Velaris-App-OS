"""Analytics answer reuse — VELARIS_CASE_ANALYTICS_CACHE_SECONDS (R6 soak, 2026-11-05).

Off (unset, every existing install): every request computes, as before.
On: an answer is reused for the same tenant scope and parameters only — two
tenants never share one, and concurrent misses compute once.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from analytics_service.api.routers import analytics as A
from tests.conftest import TestSessionFactory, deploy_case_type
from velaris_shared import shared_state
from velaris_shared.config import get_settings
from velaris_shared.db.models import CaseInstanceModel
from velaris_shared.tenancy import orm_scope

pytestmark = pytest.mark.asyncio


@pytest.fixture
def cache_seconds(monkeypatch):
    shared_state.reset_for_tests()

    def set_(n: int):
        monkeypatch.setattr(get_settings(), "analytics_cache_seconds", n, raising=False)
    yield set_
    shared_state.reset_for_tests()


async def _add_case(ct_id, tenant=None):
    async with TestSessionFactory() as db:
        db.add(CaseInstanceModel(case_type_id=ct_id, case_type_version="1.0.0", tenant_id=tenant))
        await db.commit()


async def test_off_by_default_every_request_computes(client, cache_seconds):
    assert get_settings().analytics_cache_seconds == 0      # the shipped default
    ct = await deploy_case_type(client, name="Cache off")
    await _add_case(ct["id"])
    first = (await client.get("/api/v1/analytics/dashboard")).json()["overview"]["total_cases"]
    await _add_case(ct["id"])
    second = (await client.get("/api/v1/analytics/dashboard")).json()["overview"]["total_cases"]
    assert second == first + 1


async def test_on_reuses_the_answer_within_the_window(client, cache_seconds):
    cache_seconds(30)
    ct = await deploy_case_type(client, name="Cache on")
    await _add_case(ct["id"])
    first = (await client.get("/api/v1/analytics/dashboard")).json()
    await _add_case(ct["id"])
    again = (await client.get("/api/v1/analytics/dashboard")).json()
    assert again == first                                      # reused
    other = (await client.get("/api/v1/analytics/dashboard?days=7")).json()
    assert other["overview"]["total_cases"] == first["overview"]["total_cases"] + 1  # other params: computed
    sla = (await client.get("/api/v1/analytics/sla-compliance")).json()
    assert sla["total_sla_instances"] == 0
    assert (await client.get("/api/v1/analytics/overview")).status_code == 200
    assert (await client.get("/api/v1/analytics/cases-over-time")).status_code == 200


async def test_two_tenants_never_share_an_answer(client, cache_seconds):
    cache_seconds(30)
    ct = await deploy_case_type(client, name="Cache tenants")
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _add_case(ct["id"], mine)
    for _ in range(3):
        await _add_case(ct["id"], theirs)

    async def overview(uuids):
        async with TestSessionFactory() as db:
            orm_scope.mark(db, uuids, [])
            return await A._cached(db, "overview", {"case_type_id": None}, lambda: _ov(db))

    async def _ov(db):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return A._overview_from(await A._case_rollup(db, [], today))

    a = await overview([mine])
    b = await overview([theirs])
    assert a["total_cases"] == 1
    assert b["total_cases"] == 3
    assert (await overview([mine]))["total_cases"] == 1       # still mine, from the cache


async def test_concurrent_misses_compute_once(cache_seconds):
    cache_seconds(30)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return A.SLAComplianceMetrics(total_sla_instances=7)

    async with TestSessionFactory() as db:
        orm_scope.mark(db, [uuid.uuid4()], [])
        out = await asyncio.gather(*[A._cached(db, "sla-compliance", {"x": 1}, compute)
                                     for _ in range(10)])
    assert calls == 1
    assert all(o["total_sla_instances"] == 7 for o in out)
