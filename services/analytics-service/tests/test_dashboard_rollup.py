"""Dashboard numbers from one pass — R6 soak, 2026-11-05.

The dashboard ran 19 sequential queries (total, 8 status counts, average
resolution, created/resolved today, three GROUP BYs, three assignment queries…)
and at 420k cases its p95 was 8.5 s. It now reads the cases once, grouped by
(status, priority, case type), and the assignments once. Every number must be
what the old separate queries answered.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import TestSessionFactory, deploy_case_type
from velaris_shared.db.models import CaseAssignmentModel, CaseInstanceModel

pytestmark = pytest.mark.asyncio


async def _seed(client):
    a = await deploy_case_type(client, name="Rollup A")
    b = await deploy_case_type(client, name="Rollup B")
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    # (case type, status, priority, created, resolved after hours or None)
    spec = [
        (a, "new", "low", now, None),
        (a, "open", "high", old, None),
        (a, "open", "high", old, None),
        (a, "resolved", "medium", old, 10),
        (a, "resolved", "medium", now, None),      # resolved status, no resolved_at
        (b, "closed", "critical", old, 30),
        (b, "cancelled", "low", old, None),
        (b, "reopened", "medium", now, None),
        (b, "pending_external", "high", old, None),
        (b, "pending_subcase", "high", old, None),
        (b, "on_hold", "low", old, None),          # a status the overview does not name
    ]
    async with TestSessionFactory() as db:
        cases = []
        for ct, status, prio, created, hours in spec:
            c = CaseInstanceModel(case_type_id=ct["id"], case_type_version="1.0.0",
                                  status=status, priority=prio, created_at=created,
                                  resolved_at=(created + timedelta(hours=hours)) if hours else None)
            db.add(c)
            cases.append(c)
        await db.flush()
        t0 = now - timedelta(hours=5)
        for i, (status, claimed, done_h) in enumerate([
            ("active", None, None), ("active", None, None), ("active", t0, None),
            ("completed", t0, 2), ("completed", t0, 4), ("cancelled", None, None),
        ]):
            db.add(CaseAssignmentModel(
                case_id=cases[i].id, step_id=f"s{i}", assignee_type="user", assignee_id="u",
                status=status, assigned_at=t0, claimed_at=claimed,
                completed_at=(t0 + timedelta(hours=done_h)) if done_h else None))
        await db.commit()
    return a, b


async def test_dashboard_numbers_match_the_old_queries(client):
    a, b = await _seed(client)
    d = (await client.get("/api/v1/analytics/dashboard")).json()

    o = d["overview"]
    assert o["total_cases"] == 11
    assert o["open_cases"] == 6          # new, open×2, reopened, pending_external, pending_subcase
    assert o["resolved_cases"] == 2
    assert o["closed_cases"] == 1
    assert o["cancelled_cases"] == 1
    assert o["avg_resolution_hours"] == 20.0     # (10 + 30) / 2 — only rows with resolved_at
    assert o["cases_created_today"] == 3
    # resolved_at = created + 10 h / + 30 h, both from 3 days ago: neither is today
    assert o["cases_resolved_today"] == 0

    status = {s["status"]: s["count"] for s in d["status_breakdown"]}
    assert status == {"new": 1, "open": 2, "resolved": 2, "closed": 1, "cancelled": 1,
                      "reopened": 1, "pending_external": 1, "pending_subcase": 1, "on_hold": 1}
    counts = [s["count"] for s in d["status_breakdown"]]
    assert counts == sorted(counts, reverse=True)

    assert {p["priority"]: p["count"] for p in d["priority_breakdown"]} == \
        {"low": 3, "medium": 3, "high": 4, "critical": 1}
    assert {c["case_type_name"]: c["count"] for c in d["case_type_breakdown"]} == \
        {"Rollup A": 5, "Rollup B": 6}
    assert d["case_type_breakdown"][0]["case_type_id"] == b["id"]

    asg = d["assignments"]
    assert asg["total_assignments"] == 6
    assert asg["active"] == 3
    assert asg["completed"] == 2
    assert asg["avg_completion_hours"] == 3.0
    assert asg["unassigned"] == 2


async def test_type_filter_keeps_unassigned_across_all_types(client):
    a, _ = await _seed(client)
    d = (await client.get(f"/api/v1/analytics/dashboard?case_type_id={a['id']}")).json()
    assert d["overview"]["total_cases"] == 5
    assert d["overview"]["open_cases"] == 3
    assert [c["case_type_name"] for c in d["case_type_breakdown"]] == ["Rollup A"]
    # assignments on A's five cases: active×2 unclaimed, active claimed, completed×2
    assert d["assignments"]["total_assignments"] == 5
    # …but "unassigned" was always every type's: unchanged.
    assert d["assignments"]["unassigned"] == 2


async def test_overview_endpoint_uses_the_same_numbers(client):
    await _seed(client)
    o = (await client.get("/api/v1/analytics/overview")).json()
    assert o["total_cases"] == 11
    assert o["open_cases"] == 6
    assert o["avg_resolution_hours"] == 20.0
