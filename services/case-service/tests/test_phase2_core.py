"""Phase 2 tests: urgency, SLA, assignment routing, relationships.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from tests.conftest import deploy_case_type, create_case


# ═══════════════════════════════════════════════════════════════════
# Urgency Calculator — Pure Functions
# ═══════════════════════════════════════════════════════════════════


class TestUrgencyCalculator:
    def test_priority_factor(self):
        from case_service.core.urgency_calculator import compute_priority_factor
        assert compute_priority_factor("low") == 10.0
        assert compute_priority_factor("blocker") == 50.0
        assert compute_priority_factor("unknown") == 20.0

    def test_sla_proximity_no_slas(self):
        from case_service.core.urgency_calculator import compute_sla_proximity_factor
        assert compute_sla_proximity_factor([]) == 0.0

    def test_sla_proximity_halfway(self):
        from case_service.core.urgency_calculator import compute_sla_proximity_factor
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        sla = {
            "status": "on_track",
            "started_at": datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
            "deadline_at": datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc),
            "paused_duration_seconds": 0,
        }
        factor = compute_sla_proximity_factor([sla], now=now)
        assert 0.49 < factor < 0.51

    def test_sla_proximity_breached(self):
        from case_service.core.urgency_calculator import compute_sla_proximity_factor
        assert compute_sla_proximity_factor([{"status": "breached"}]) == 1.5

    def test_age_factor(self):
        from case_service.core.urgency_calculator import compute_age_factor
        now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
        created = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        assert 1.99 < compute_age_factor(created, now=now) < 2.01

    def test_blocking_factor(self):
        from case_service.core.urgency_calculator import compute_blocking_factor
        assert compute_blocking_factor(0) == 0.0
        assert compute_blocking_factor(3) == 30.0

    def test_full_urgency(self):
        from case_service.core.urgency_calculator import compute_urgency
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        score = compute_urgency(
            priority="high",
            sla_instances=[{
                "status": "on_track",
                "started_at": datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
                "deadline_at": datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc),
                "paused_duration_seconds": 0,
            }],
            created_at=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
            blocking_count=2,
            now=now,
        )
        assert score > 60

    def test_custom_weights(self):
        from case_service.core.urgency_calculator import compute_urgency
        s1 = compute_urgency(priority="high")
        s2 = compute_urgency(priority="high", weights={"priority": 5.0, "sla": 0, "age": 0, "relationship": 0})
        assert s2 > s1


# ═══════════════════════════════════════════════════════════════════
# SLA Duration Parsing
# ═══════════════════════════════════════════════════════════════════


class TestSLADurationParsing:
    def test_hours(self):
        from case_service.core.sla_tracker import parse_iso8601_duration
        assert parse_iso8601_duration("PT4H") == timedelta(hours=4)

    def test_days(self):
        from case_service.core.sla_tracker import parse_iso8601_duration
        assert parse_iso8601_duration("P2D") == timedelta(days=2)

    def test_complex(self):
        from case_service.core.sla_tracker import parse_iso8601_duration
        assert parse_iso8601_duration("P1DT6H30M") == timedelta(days=1, hours=6, minutes=30)

    def test_seconds(self):
        from case_service.core.sla_tracker import parse_iso8601_duration
        assert parse_iso8601_duration("PT30S") == timedelta(seconds=30)

    def test_invalid(self):
        from case_service.core.sla_tracker import parse_iso8601_duration
        with pytest.raises(ValueError):
            parse_iso8601_duration("invalid")

    def test_to_seconds(self):
        from case_service.core.sla_tracker import duration_to_seconds
        assert duration_to_seconds("PT1H") == 3600


# ═══════════════════════════════════════════════════════════════════
# SLA Tracker — DB Operations
# ═══════════════════════════════════════════════════════════════════


class TestSLATrackerDB:
    async def test_start_sla(self, session, client):
        from case_service.core.sla_tracker import start_sla
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        now = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)

        sla = await start_sla(
            session, case_id=uuid.UUID(case["id"]),
            sla_policy={"id": "sla-1", "goal_duration": "PT4H", "deadline_duration": "PT8H"},
            target_id=case["id"], now=now,
        )
        await session.commit()
        assert sla.status == "on_track"

    async def test_pause_and_resume(self, session, client):
        from case_service.core.sla_tracker import start_sla, pause_sla, resume_sla
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        cid = uuid.UUID(case["id"])
        t0 = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)

        await start_sla(session, case_id=cid,
                        sla_policy={"id": "sla-p", "goal_duration": "PT4H", "deadline_duration": "PT8H"},
                        target_id=case["id"], now=t0)
        await session.commit()

        assert await pause_sla(session, case_id=cid, sla_policy_id="sla-p", now=t0 + timedelta(hours=1))
        await session.commit()

        assert await resume_sla(session, case_id=cid, sla_policy_id="sla-p", now=t0 + timedelta(hours=3))
        await session.commit()

    async def test_check_at_risk(self, session, client):
        from case_service.core.sla_tracker import start_sla, check_sla
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        cid = uuid.UUID(case["id"])
        t0 = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)

        await start_sla(session, case_id=cid,
                        sla_policy={"id": "sla-ar", "goal_duration": "PT4H", "deadline_duration": "PT8H"},
                        target_id=case["id"], now=t0)
        await session.commit()

        status = await check_sla(session, case_id=cid, sla_policy_id="sla-ar", now=t0 + timedelta(hours=5))
        await session.commit()
        assert status["status"] == "at_risk"

    async def test_check_breached(self, session, client):
        from case_service.core.sla_tracker import start_sla, check_sla
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        cid = uuid.UUID(case["id"])
        t0 = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)

        await start_sla(session, case_id=cid,
                        sla_policy={"id": "sla-b", "goal_duration": "PT2H", "deadline_duration": "PT4H"},
                        target_id=case["id"], now=t0)
        await session.commit()

        status = await check_sla(session, case_id=cid, sla_policy_id="sla-b", now=t0 + timedelta(hours=5))
        await session.commit()
        assert status["status"] == "breached"


# ═══════════════════════════════════════════════════════════════════
# Assignment Router
# ═══════════════════════════════════════════════════════════════════


class TestAssignmentRouter:
    async def test_default(self, session):
        from case_service.core.assignment_router import resolve_assignment
        atype, aid = await resolve_assignment(session, None)
        assert atype == "queue"

    async def test_specific_user(self, session):
        from case_service.core.assignment_router import resolve_assignment
        atype, aid = await resolve_assignment(session, {"strategy": "specific_user", "target": "u-1"})
        assert (atype, aid) == ("user", "u-1")

    async def test_role_based(self, session):
        from case_service.core.assignment_router import resolve_assignment
        atype, aid = await resolve_assignment(session, {"strategy": "role_based", "target": "adjuster"})
        assert (atype, aid) == ("role", "adjuster")

    async def test_fallback(self, session):
        from case_service.core.assignment_router import resolve_assignment
        rule = {
            "strategy": "specific_user", "target": "unassigned",
            "fallback_strategy": "queue_based", "fallback_target": "overflow",
        }
        atype, aid = await resolve_assignment(session, rule)
        assert (atype, aid) == ("queue", "overflow")

    async def test_create_for_step(self, session, client):
        from case_service.core.assignment_router import create_assignment_for_step
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        step = {
            "id": "step-1", "name": "Review", "step_type": "user_task",
            "bpmn_element_id": "t1",
            "assignment": {"strategy": "role_based", "target": "reviewer"},
        }
        a = await create_assignment_for_step(session, case_id=uuid.UUID(case["id"]), step=step)
        await session.commit()
        assert a.assignee_type == "role"
        assert a.assignee_id == "reviewer"


# ═══════════════════════════════════════════════════════════════════
# Relationship Manager
# ═══════════════════════════════════════════════════════════════════


class TestRelationshipManager:
    async def test_priority_propagation(self, client):
        ct = await deploy_case_type(client)
        parent = await create_case(client, ct["id"], priority="low")
        child = await create_case(client, ct["id"], priority="low")

        await client.post(f"/api/v1/cases/{parent['id']}/relationships", json={
            "target_case_id": child["id"], "relationship_type": "child",
            "propagate_priority": True,
        })
        await client.post(f"/api/v1/cases/{child['id']}/priority", json={"priority": "critical"})

        resp = await client.get(f"/api/v1/cases/{parent['id']}")
        assert resp.json()["priority"] == "critical"

    async def test_status_propagation_unblocks_parent(self, client):
        ct = await deploy_case_type(client)
        parent = await create_case(client, ct["id"])
        child = await create_case(client, ct["id"])

        await client.post(f"/api/v1/cases/{parent['id']}/status", json={"status": "pending_subcase"})
        await client.post(f"/api/v1/cases/{parent['id']}/relationships", json={
            "target_case_id": child["id"], "relationship_type": "child",
            "propagate_status": True, "required": True,
        })
        await client.post(f"/api/v1/cases/{child['id']}/resolve", json={})

        resp = await client.get(f"/api/v1/cases/{parent['id']}")
        assert resp.json()["status"] == "open"


# ═══════════════════════════════════════════════════════════════════
# Lifecycle Integration
# ═══════════════════════════════════════════════════════════════════


class TestLifecycleIntegration:
    async def test_urgency_updates_on_priority_change(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"], priority="low")
        initial = (await client.get(f"/api/v1/cases/{case['id']}")).json()["urgency_score"]

        await client.post(f"/api/v1/cases/{case['id']}/priority", json={"priority": "blocker"})
        updated = (await client.get(f"/api/v1/cases/{case['id']}")).json()["urgency_score"]
        assert updated > initial

    async def test_full_lifecycle_smoke(self, client):
        ct = await deploy_case_type(client, name="Smoke", version="1.0.0")
        case = await create_case(client, ct["id"])
        assert case["status"] == "new"

        r = await client.post(f"/api/v1/cases/{case['id']}/status", json={"status": "open"})
        assert r.json()["status"] == "open"
        r = await client.post(f"/api/v1/cases/{case['id']}/resolve", json={})
        assert r.json()["status"] == "resolved"
        r = await client.post(f"/api/v1/cases/{case['id']}/close", json={})
        assert r.json()["status"] == "closed"
