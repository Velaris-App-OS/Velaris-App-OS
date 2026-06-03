"""Phase 14 tests — Process Mining.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest


class TestEventLogger:
    async def test_log_event_basic(self, client, session):
        from case_service.process_mining.event_logger import log_event
        ct = await client.post("/api/v1/case-types", json={
            "name": f"PM-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-proc",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        case = await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"], "data": {},
        })

        await log_event(
            session,
            case_id=uuid.UUID(case.json()["id"]),
            case_type_id=uuid.UUID(ct.json()["id"]),
            activity="test_activity",
            activity_type="step_complete",
        )
        await session.commit()

    async def test_log_case_lifecycle(self, client, session):
        from case_service.process_mining.event_logger import (
            log_case_created, log_case_resolved, log_step_completed,
        )
        ct = await client.post("/api/v1/case-types", json={
            "name": f"LC-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        case = await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"], "data": {},
        })
        ct_id = uuid.UUID(ct.json()["id"])
        case_id = uuid.UUID(case.json()["id"])

        await log_case_created(session, case_id, ct_id)
        await log_step_completed(session, case_id, ct_id, "review", duration_seconds=120)
        await log_case_resolved(session, case_id, ct_id, duration_seconds=300)
        await session.commit()


class TestProcessMiningAPI:
    async def test_summary_empty(self, client):
        resp = await client.get("/api/v1/process-mining/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_events"] == 0

    async def test_activity_stats_empty(self, client):
        resp = await client.get("/api/v1/process-mining/activity-stats")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_bottlenecks_empty(self, client):
        resp = await client.get("/api/v1/process-mining/bottlenecks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_variants_empty(self, client):
        resp = await client.get("/api/v1/process-mining/variants")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_flow_graph_empty(self, client):
        resp = await client.get("/api/v1/process-mining/flow-graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data

    async def test_duration_stats_empty(self, client):
        resp = await client.get("/api/v1/process-mining/duration-stats")
        assert resp.status_code == 200
        assert resp.json()["cases_analyzed"] == 0

    async def test_events_empty(self, client):
        resp = await client.get("/api/v1/process-mining/events")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_conformance_not_found(self, client):
        resp = await client.get(f"/api/v1/process-mining/conformance/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestAnalyzerFunctions:
    async def test_bottleneck_severity_classification(self):
        from case_service.process_mining.analyzer import _classify_severity
        assert _classify_severity(100) == "low"
        assert _classify_severity(7200) == "medium"
        assert _classify_severity(18000) == "high"
        assert _classify_severity(100000) == "critical"

    async def test_activity_stats_with_data(self, client, session):
        from case_service.process_mining.event_logger import log_event

        ct = await client.post("/api/v1/case-types", json={
            "name": f"AS-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        case = await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"], "data": {},
        })
        ct_id = uuid.UUID(ct.json()["id"])
        case_id = uuid.UUID(case.json()["id"])

        # Log multiple events
        for _ in range(3):
            await log_event(
                session, case_id=case_id, case_type_id=ct_id,
                activity="review", activity_type="step_complete",
                duration_seconds=60,
            )
        await session.commit()

        resp = await client.get("/api/v1/process-mining/activity-stats")
        stats = resp.json()
        # At least our test activity should be in there
        review_stats = [s for s in stats if s["activity"] == "review"]
        assert len(review_stats) >= 1
        assert review_stats[0]["count"] >= 3

    async def test_variant_discovery(self, client, session):
        from case_service.process_mining.event_logger import log_event

        ct = await client.post("/api/v1/case-types", json={
            "name": f"VD-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        ct_id = uuid.UUID(ct.json()["id"])

        # Create 2 cases with same path
        for _ in range(2):
            case = await client.post("/api/v1/cases", json={
                "case_type_id": ct.json()["id"], "data": {},
            })
            case_id = uuid.UUID(case.json()["id"])
            await log_event(session, case_id=case_id, case_type_id=ct_id, activity="A", activity_type="step")
            await log_event(session, case_id=case_id, case_type_id=ct_id, activity="B", activity_type="step")
            await log_event(session, case_id=case_id, case_type_id=ct_id, activity="C", activity_type="step")
        await session.commit()

        resp = await client.get("/api/v1/process-mining/variants")
        variants = resp.json()
        assert len(variants) >= 1
        # Top variant should have both cases
        assert variants[0]["case_count"] >= 2

    async def test_flow_graph_with_data(self, client, session):
        from case_service.process_mining.event_logger import log_event

        ct = await client.post("/api/v1/case-types", json={
            "name": f"FG-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        ct_id = uuid.UUID(ct.json()["id"])
        case = await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"], "data": {},
        })
        case_id = uuid.UUID(case.json()["id"])

        await log_event(session, case_id=case_id, case_type_id=ct_id, activity="start", activity_type="s")
        await log_event(session, case_id=case_id, case_type_id=ct_id, activity="middle", activity_type="s")
        await log_event(session, case_id=case_id, case_type_id=ct_id, activity="end", activity_type="s")
        await session.commit()

        resp = await client.get("/api/v1/process-mining/flow-graph")
        dfg = resp.json()
        assert len(dfg["edges"]) >= 2  # start→middle, middle→end
