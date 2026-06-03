"""Phase 9 tests — Analytics API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest


class TestAnalyticsDashboard:
    async def test_empty_dashboard(self, client):
        resp = await client.get("/api/v1/analytics/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overview"]["total_cases"] == 0
        assert data["overview"]["open_cases"] == 0
        assert data["sla_compliance"]["total_sla_instances"] == 0
        assert data["assignments"]["total_assignments"] == 0
        assert isinstance(data["status_breakdown"], list)
        assert isinstance(data["cases_over_time"], list)

    async def test_dashboard_with_cases(self, client):
        # Create a case type and some cases
        ct = await client.post("/api/v1/case-types", json={
            "name": f"Analytics-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-proc",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        ct_id = ct.json()["id"]

        for i in range(5):
            await client.post("/api/v1/cases", json={
                "case_type_id": ct_id,
                "data": {"index": i},
                "priority": ["low", "medium", "high", "critical", "medium"][i],
            })

        resp = await client.get("/api/v1/analytics/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overview"]["total_cases"] == 5
        assert len(data["priority_breakdown"]) > 0
        assert len(data["case_type_breakdown"]) == 1
        assert data["case_type_breakdown"][0]["count"] == 5

    async def test_dashboard_with_type_filter(self, client):
        ct1 = await client.post("/api/v1/case-types", json={
            "name": f"TypeA-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-proc",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        ct2 = await client.post("/api/v1/case-types", json={
            "name": f"TypeB-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-proc",
            "definition_json": {"stages": [], "sla_policies": []},
        })

        for _ in range(3):
            await client.post("/api/v1/cases", json={"case_type_id": ct1.json()["id"], "data": {}})
        for _ in range(2):
            await client.post("/api/v1/cases", json={"case_type_id": ct2.json()["id"], "data": {}})

        # Unfiltered
        resp = await client.get("/api/v1/analytics/dashboard")
        assert resp.json()["overview"]["total_cases"] == 5

        # Filtered to TypeA
        resp2 = await client.get(f"/api/v1/analytics/dashboard?case_type_id={ct1.json()['id']}")
        assert resp2.json()["overview"]["total_cases"] == 3

    async def test_dashboard_days_param(self, client):
        resp = await client.get("/api/v1/analytics/dashboard?days=7")
        assert resp.status_code == 200

        resp2 = await client.get("/api/v1/analytics/dashboard?days=90")
        assert resp2.status_code == 200

    async def test_status_breakdown(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"StatusTest-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-proc",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        ct_id = ct.json()["id"]

        # Create cases with different statuses
        c1 = await client.post("/api/v1/cases", json={"case_type_id": ct_id, "data": {}})
        c2 = await client.post("/api/v1/cases", json={"case_type_id": ct_id, "data": {}})
        c3 = await client.post("/api/v1/cases", json={"case_type_id": ct_id, "data": {}})

        # Resolve one
        await client.post(f"/api/v1/cases/{c2.json()['id']}/resolve", json={})

        resp = await client.get("/api/v1/analytics/dashboard")
        data = resp.json()
        statuses = {s["status"]: s["count"] for s in data["status_breakdown"]}
        assert statuses.get("new", 0) >= 2
        assert statuses.get("resolved", 0) >= 1


class TestOverviewEndpoint:
    async def test_overview_standalone(self, client):
        resp = await client.get("/api/v1/analytics/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_cases" in data
        assert "open_cases" in data
        assert "avg_resolution_hours" in data


class TestSLAComplianceEndpoint:
    async def test_sla_compliance_empty(self, client):
        resp = await client.get("/api/v1/analytics/sla-compliance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sla_instances"] == 0
        assert data["compliance_rate"] == 0.0


class TestCasesOverTimeEndpoint:
    async def test_cases_over_time(self, client):
        resp = await client.get("/api/v1/analytics/cases-over-time?days=7")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_cases_over_time_with_data(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"Time-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-proc",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        for _ in range(3):
            await client.post("/api/v1/cases", json={"case_type_id": ct.json()["id"], "data": {}})

        resp = await client.get("/api/v1/analytics/cases-over-time?days=1")
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["count"] >= 3
