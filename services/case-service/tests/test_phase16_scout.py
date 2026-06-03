"""Phase 16 tests — Scout Migration Scanner.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest


SAMPLE_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <bpmn:process id="approval" name="Approval Process">
    <bpmn:startEvent id="start" name="Start"/>
    <bpmn:userTask id="review" name="Review"/>
    <bpmn:exclusiveGateway id="gw" name="Approved?"/>
    <bpmn:userTask id="approve" name="Approve"/>
    <bpmn:serviceTask id="notify" name="Notify"/>
    <bpmn:endEvent id="end" name="End"/>
  </bpmn:process>
</bpmn:definitions>"""

SAMPLE_PEGA = """
Rule-Obj-CaseType: InsuranceClaim
Rule-Obj-Flow: ClaimIntakeFlow
Rule-HTML-Section: ClaimDetails
Rule-Declare-DecisionTable: CoverageDecision
Rule-Obj-Activity: CalculatePayout
Rule-Connect-REST: PaymentServiceCall
Rule-Access-Role-Obj: ClaimsAdjuster
Rule-Obj-SLA: ResolutionSLA
"""

SAMPLE_APPIAN = """
{
  "objectType": "process_model",
  "name": "LoanApplicationProcess"
}
{
  "objectType": "interface",
  "name": "ApplicationForm"
}
{
  "objectType": "expression_rule",
  "name": "CreditScoreRule"
}
"""


class TestBaseModels:
    def test_scan_result_empty(self):
        from case_service.scout.base import ScanResult
        r = ScanResult(source_platform="test")
        assert r.total_artifacts == 0
        assert r.compatibility_score == 0.0
        assert r.effort_weeks == 1  # Minimum 1

    def test_compatibility_score_calculation(self):
        from case_service.scout.base import (
            ScanResult, ScannedArtifact, ArtifactType, CompatibilityLevel,
        )
        r = ScanResult(source_platform="test")
        r.artifacts = [
            ScannedArtifact(ArtifactType.PROCESS, "P1", "id1", CompatibilityLevel.FULL),
            ScannedArtifact(ArtifactType.PROCESS, "P2", "id2", CompatibilityLevel.HIGH),
            ScannedArtifact(ArtifactType.PROCESS, "P3", "id3", CompatibilityLevel.MEDIUM),
        ]
        score = r.compatibility_score
        assert 0.7 <= score <= 0.9


class TestBPMNScanner:
    def test_scan_simple_bpmn(self):
        from case_service.scout.camunda_scanner import scan_camunda_bpmn
        result = scan_camunda_bpmn(SAMPLE_BPMN)
        assert result.source_platform == "camunda"
        assert result.total_artifacts > 0
        # Should find process, startEvent, userTasks, etc.
        types = {a.artifact_type.value for a in result.artifacts}
        assert "process" in types or "workflow" in types

    def test_scan_detects_user_tasks(self):
        from case_service.scout.camunda_scanner import scan_camunda_bpmn
        result = scan_camunda_bpmn(SAMPLE_BPMN)
        user_tasks = [a for a in result.artifacts if "userTask" in str(a.metadata.get("bpmn_element", ""))]
        assert len(user_tasks) >= 2  # review + approve

    def test_scan_detects_gateway(self):
        from case_service.scout.camunda_scanner import scan_camunda_bpmn
        result = scan_camunda_bpmn(SAMPLE_BPMN)
        gateways = [a for a in result.artifacts if "gateway" in str(a.metadata.get("bpmn_element", "")).lower()]
        assert len(gateways) >= 1


class TestPegaScanner:
    def test_scan_pega_rules(self):
        from case_service.scout.pega_scanner import scan_pega_export
        result = scan_pega_export(SAMPLE_PEGA)
        assert result.source_platform == "pega"
        assert result.total_artifacts >= 5
        # Should find case type, flow, section, etc.
        types = {a.artifact_type.value for a in result.artifacts}
        assert "case_type" in types

    def test_pega_sla_full_compat(self):
        from case_service.scout.pega_scanner import scan_pega_export
        from case_service.scout.base import CompatibilityLevel
        result = scan_pega_export(SAMPLE_PEGA)
        sla_artifacts = [a for a in result.artifacts if "SLA" in a.name]
        assert len(sla_artifacts) >= 1
        assert sla_artifacts[0].compatibility == CompatibilityLevel.FULL

    def test_pega_decision_table_full_compat(self):
        from case_service.scout.pega_scanner import scan_pega_export
        from case_service.scout.base import CompatibilityLevel
        result = scan_pega_export(SAMPLE_PEGA)
        dt = [a for a in result.artifacts if "Decision" in a.name or "decision_table" == a.artifact_type.value]
        assert len(dt) >= 1


class TestAppianScanner:
    def test_scan_appian_objects(self):
        from case_service.scout.appian_scanner import scan_appian_export
        result = scan_appian_export(SAMPLE_APPIAN)
        assert result.source_platform == "appian"
        assert result.total_artifacts >= 2


class TestPlatformDetection:
    def test_detect_bpmn(self):
        from case_service.scout.scanner import detect_source_platform
        assert detect_source_platform(SAMPLE_BPMN) == "camunda"

    def test_detect_by_filename(self):
        from case_service.scout.scanner import detect_source_platform
        assert detect_source_platform("", "process.bpmn") == "camunda"
        assert detect_source_platform("", "export.rap") == "pega"
        assert detect_source_platform("some content", "appian-export.zip") == "appian"


class TestMigrationPlanner:
    def test_build_plan(self):
        from case_service.scout.scanner import scan
        from case_service.scout.migration_planner import build_migration_plan
        result = scan(SAMPLE_BPMN, "camunda")
        plan = build_migration_plan(result)
        assert "summary" in plan
        assert "phases" in plan
        assert len(plan["phases"]) == 4
        assert plan["summary"]["total_artifacts"] > 0


class TestScoutAPI:
    async def test_list_platforms(self, client):
        resp = await client.get("/api/v1/scout/platforms")
        assert resp.status_code == 200
        platforms = resp.json()["platforms"]
        platform_ids = [p["id"] for p in platforms]
        assert "pega" in platform_ids
        assert "camunda" in platform_ids
        assert "appian" in platform_ids

    async def test_scan_bpmn(self, client):
        resp = await client.post("/api/v1/scout/scan", json={
            "name": "Test BPMN Scan",
            "content": SAMPLE_BPMN,
            "filename": "test.bpmn",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_platform"] == "camunda"
        assert data["compatibility_score"] is not None

    async def test_scan_pega(self, client):
        resp = await client.post("/api/v1/scout/scan", json={
            "name": "Test Pega Scan",
            "content": SAMPLE_PEGA,
            "source_platform": "pega",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_platform"] == "pega"

    async def test_list_scans(self, client):
        # Create a scan first
        await client.post("/api/v1/scout/scan", json={
            "name": "ListTest", "content": SAMPLE_BPMN, "source_platform": "camunda",
        })
        resp = await client.get("/api/v1/scout/scans")
        assert resp.status_code == 200
        scans = resp.json()
        assert len(scans) >= 1

    async def test_get_scan_detail(self, client):
        create = await client.post("/api/v1/scout/scan", json={
            "name": "DetailTest", "content": SAMPLE_BPMN, "source_platform": "camunda",
        })
        scan_id = create.json()["id"]

        resp = await client.get(f"/api/v1/scout/scans/{scan_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "DetailTest"

    async def test_get_migration_plan(self, client):
        create = await client.post("/api/v1/scout/scan", json={
            "name": "PlanTest", "content": SAMPLE_BPMN, "source_platform": "camunda",
        })
        scan_id = create.json()["id"]

        resp = await client.get(f"/api/v1/scout/scans/{scan_id}/plan")
        assert resp.status_code == 200
        plan = resp.json()
        assert "phases" in plan
        assert "summary" in plan

    async def test_delete_scan(self, client):
        create = await client.post("/api/v1/scout/scan", json={
            "name": "DeleteTest", "content": SAMPLE_BPMN, "source_platform": "camunda",
        })
        scan_id = create.json()["id"]

        resp = await client.delete(f"/api/v1/scout/scans/{scan_id}")
        assert resp.status_code == 204

    async def test_scan_not_found(self, client):
        resp = await client.get(f"/api/v1/scout/scans/{uuid.uuid4()}")
        assert resp.status_code == 404
