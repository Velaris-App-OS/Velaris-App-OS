"""S2 (R6 soak 2026-11-02): refused repeats are counted, not written one by one.

Every refused (401/403) mutating request wrote a security_events row: 60,724
rows in 1.5 h of an unauthenticated flood. Now the first refusal of a key
(address, actor, route, status) is written as before; repeats inside a minute
become ONE summary row with `repeat_count` (migration 231). Every count of
events reads SUM(repeat_count), so the totals are what they were.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from tests.conftest import TestSessionFactory
from velaris_shared.db import security_events
from velaris_shared.db.models import SecurityEventModel
from velaris_shared.middleware import audit

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _short_window(monkeypatch):
    audit._repeats.clear()
    monkeypatch.setattr(audit, "REPEAT_WINDOW", 0.3)
    yield
    audit._repeats.clear()


async def _rows():
    async with TestSessionFactory() as db:
        rows = (await db.execute(
            select(SecurityEventModel).where(SecurityEventModel.outcome == "denied",
                                             SecurityEventModel.event_type == "api.post")
        )).scalars().all()
        return sorted(rows, key=lambda r: r.repeat_count)


async def test_a_flood_of_refusals_is_two_rows_that_count_all_of_them(anon_client):
    for _ in range(50):
        r = await anon_client.post("/api/v1/cases", json={})
        assert r.status_code == 401            # still refused, every time
    await asyncio.sleep(0.1)
    rows = await _rows()
    assert [r.repeat_count for r in rows] == [1]          # only the first written so far

    await asyncio.sleep(0.6)                               # the window closes
    rows = await _rows()
    assert [r.repeat_count for r in rows] == [1, 49]
    summary = rows[1].details
    assert summary["repeats"] == 49 and summary["first_at"] <= summary["last_at"]
    assert summary["path"] == "/api/v1/cases" and summary["status"] == 401

    async with TestSessionFactory() as db:
        stats = await security_events.event_stats(db, 1)
    assert stats["by_type"]["api.post"] == 50
    assert stats["failed_count"] >= 50


async def test_a_new_window_starts_after_the_old_one_closed(anon_client):
    await anon_client.post("/api/v1/cases", json={})
    await asyncio.sleep(0.6)
    await anon_client.post("/api/v1/cases", json={})
    await asyncio.sleep(0.1)
    assert [r.repeat_count for r in await _rows()] == [1, 1]   # no repeats: no summary


async def test_successful_calls_are_never_folded(client):
    from tests.conftest import deploy_case_type
    ct = await deploy_case_type(client, name="Fold check")
    before = len(audit._repeats)
    for _ in range(3):
        r = await client.post("/api/v1/cases", json={"case_type_id": ct["id"], "data": {}})
        assert r.status_code in (200, 201)
    assert len(audit._repeats) == before


async def test_past_the_key_limit_every_refusal_is_written(anon_client, monkeypatch):
    monkeypatch.setattr(audit, "MAX_REPEAT_KEYS", 0)
    for _ in range(5):
        await anon_client.post("/api/v1/cases", json={})
    await asyncio.sleep(0.1)
    assert [r.repeat_count for r in await _rows()] == [1] * 5   # as before 231: never dropped
