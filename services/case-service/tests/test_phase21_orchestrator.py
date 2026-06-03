"""Phase 21 tests — Migration Orchestrator.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest


SAMPLE_SCAN_CONTENT = """
Rule-Obj-CaseType: InsuranceClaim
Rule-Obj-Flow: ClaimIntakeFlow
Rule-HTML-Section: ClaimDetails
Rule-Declare-DecisionTable: CoverageDecision
Rule-Obj-Activity: CalculatePayout
Rule-Connect-REST: PaymentServiceCall
"""


class TestDependencyGraph:
    def test_empty_graph(self):
        from case_service.orchestrator.dependency_graph import build_dependencies
        deps = build_dependencies([])
        assert deps == {}

    def test_simple_graph(self):
        from case_service.orchestrator.dependency_graph import build_dependencies
        artifacts = [
            {"identifier": "cm1", "type": "data_model", "name": "Claim"},
            {"identifier": "cf1", "type": "form", "name": "Claim Form"},
        ]
        deps = build_dependencies(artifacts)
        # Form should depend on data model (name overlap)
        assert "cm1" in deps.get("cf1", [])

    def test_topological_sort(self):
        from case_service.orchestrator.dependency_graph import (
            build_dependencies, topological_sort,
        )
        artifacts = [
            {"identifier": "a", "type": "data_model", "name": "Base"},
            {"identifier": "b", "type": "form", "name": "Base Form"},
        ]
        deps = build_dependencies(artifacts)
        ordered = topological_sort(artifacts, deps)
        # data_model should come before form
        assert ordered.index("a") < ordered.index("b")

    def test_phase_assignment(self):
        from case_service.orchestrator.dependency_graph import phase_for_compatibility
        assert phase_for_compatibility("full") == 1
        assert phase_for_compatibility("high") == 2
        assert phase_for_compatibility("medium") == 3
        assert phase_for_compatibility("low") == 4
        assert phase_for_compatibility("incompatible") == 4


class TestProjectCreation:
    async def test_create_project_from_scan(self, client):
        # First create a scan
        scan_resp = await client.post("/api/v1/scout/scan", json={
            "name": "OrchTest Scan",
            "content": SAMPLE_SCAN_CONTENT,
            "source_platform": "pega",
        })
        scan_id = scan_resp.json()["id"]

        # Create project
        resp = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Test Migration",
            "scan_id": scan_id,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Migration"
        assert data["total_artifacts"] > 0

    async def test_invalid_scan_id(self, client):
        resp = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Bad Scan",
            "scan_id": str(uuid.uuid4()),
        })
        assert resp.status_code == 404


class TestProjectQueries:
    async def test_list_projects(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "ListTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        await client.post("/api/v1/orchestrator/projects", json={
            "name": "List Project", "scan_id": scan.json()["id"],
        })

        resp = await client.get("/api/v1/orchestrator/projects")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_get_project_detail(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "DetailTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Detail Project", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]

        resp = await client.get(f"/api/v1/orchestrator/projects/{pid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Detail Project"


class TestTasks:
    async def test_list_tasks(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "TaskTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Task Project", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]

        resp = await client.get(f"/api/v1/orchestrator/projects/{pid}/tasks")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) > 0
        # Should have phase + sequence assigned
        assert all("phase" in t for t in tasks)

    async def test_analyze_single_task(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "AnalyzeTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Analyze Project", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]

        tasks = await client.get(f"/api/v1/orchestrator/projects/{pid}/tasks")
        if not tasks.json():
            return
        task_id = tasks.json()[0]["id"]

        resp = await client.post(f"/api/v1/orchestrator/tasks/{task_id}/analyze")
        assert resp.status_code == 200
        assert "analysis_id" in resp.json()

    async def test_generate_code_for_task(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "GenTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Gen Project", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]

        tasks = await client.get(f"/api/v1/orchestrator/projects/{pid}/tasks")
        if not tasks.json():
            return
        task_id = tasks.json()[0]["id"]

        # Analyze first
        await client.post(f"/api/v1/orchestrator/tasks/{task_id}/analyze")
        # Generate
        resp = await client.post(f"/api/v1/orchestrator/tasks/{task_id}/generate")
        assert resp.status_code == 200
        assert "generated_code" in resp.json()
        assert len(resp.json()["generated_code"]) > 0

    async def test_mark_task_ported(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "PortTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Port Project", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]
        tasks = await client.get(f"/api/v1/orchestrator/projects/{pid}/tasks")
        if not tasks.json():
            return
        task_id = tasks.json()[0]["id"]

        resp = await client.post(f"/api/v1/orchestrator/tasks/{task_id}/mark-ported")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ported"


class TestFullPipeline:
    async def test_run_all(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "FullPipe", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Full Pipeline", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]

        resp = await client.post(f"/api/v1/orchestrator/projects/{pid}/run-all?max_tasks=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "analyzed" in data
        assert "generated" in data


class TestRoadmap:
    async def test_get_roadmap(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "RoadTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Road Project", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]

        resp = await client.get(f"/api/v1/orchestrator/projects/{pid}/roadmap")
        assert resp.status_code == 200
        roadmap = resp.json()
        assert "phases" in roadmap
        assert roadmap["project_name"] == "Road Project"


class TestExport:
    async def test_export_zip(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "ExportTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Export Project", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]

        resp = await client.get(f"/api/v1/orchestrator/projects/{pid}/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"


class TestProjectDeletion:
    async def test_delete_project(self, client):
        scan = await client.post("/api/v1/scout/scan", json={
            "name": "DelTest", "content": SAMPLE_SCAN_CONTENT, "source_platform": "pega",
        })
        create = await client.post("/api/v1/orchestrator/projects", json={
            "name": "Delete Project", "scan_id": scan.json()["id"],
        })
        pid = create.json()["id"]

        resp = await client.delete(f"/api/v1/orchestrator/projects/{pid}")
        assert resp.status_code == 204

        get_resp = await client.get(f"/api/v1/orchestrator/projects/{pid}")
        assert get_resp.status_code == 404

    async def test_delete_nonexistent(self, client):
        resp = await client.delete(f"/api/v1/orchestrator/projects/{uuid.uuid4()}")
        assert resp.status_code == 404
