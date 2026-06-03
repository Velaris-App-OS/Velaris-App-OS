"""Phase 19 tests — AI-Powered Scout.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest


SAMPLE_PEGA_CODE = """
Property.pyStatus = "Processing";
Page PaymentDetails = tools.getProperty("PaymentDetails");
double amount = PaymentDetails.getDouble("Amount");
double adjusted = amount * 0.95;
tools.putProperty("NetAmount", adjusted);
ConnectREST conn = tools.getConnectRest("PaymentServiceCall");
conn.setRequestParam("amount", adjusted);
conn.execute();
tools.logAudit("Payout: " + adjusted);
if (adjusted > 10000) {
    tools.sendEmail("manager@company.com", "Large payout");
}
"""

SAMPLE_CAMUNDA_CODE = """
var order = execution.getVariable("order");
var customerId = order.customerId;
var response = connector.get("/api/customers/123");
if (response.creditScore < 600) {
    execution.setVariable("approval_required", true);
}
"""


class TestHeuristicAnalyzer:
    def test_heuristic_analyze_pega(self):
        from case_service.scout.ai.analyzer import _heuristic_analyze
        result = _heuristic_analyze(SAMPLE_PEGA_CODE, "activity", "pega")
        assert result.source == "heuristic"
        # Should detect REST call
        assert any("REST" in c for c in result.external_calls)
        # Should detect email side effect
        assert any("email" in s.lower() for s in result.side_effects)
        assert any("audit" in s.lower() for s in result.side_effects)

    def test_heuristic_analyze_camunda(self):
        from case_service.scout.ai.analyzer import _heuristic_analyze
        result = _heuristic_analyze(SAMPLE_CAMUNDA_CODE, "script", "camunda")
        assert result.source == "heuristic"
        assert "conditional" in result.business_logic.lower() or "branch" in result.business_logic.lower()

    def test_complexity_classification(self):
        from case_service.scout.ai.analyzer import _classify_complexity
        assert _classify_complexity("x = 1", 1) == "low"
        assert _classify_complexity("line\n" * 60, 60) == "medium"
        assert _classify_complexity("line\n" * 200, 200) == "high"
        assert _classify_complexity("line\n" * 600, 600) == "extreme"

    def test_helix_mapping_for_integration(self):
        from case_service.scout.ai.analyzer import _heuristic_analyze
        result = _heuristic_analyze(SAMPLE_PEGA_CODE, "activity", "pega")
        assert result.helix_mapping["artifact_type"] == "integration"

    def test_helix_mapping_for_decision(self):
        from case_service.scout.ai.analyzer import _heuristic_analyze
        heavy_conditions = "\n".join(f"if (x == {i}) {{ y = {i}; }}" for i in range(10))
        result = _heuristic_analyze(heavy_conditions, "rule", "pega")
        assert result.helix_mapping["artifact_type"] == "decision_table"


class TestCodeGeneration:
    def test_heuristic_generate_code(self):
        from case_service.scout.ai.analyzer import (
            _heuristic_generate_code, ArtifactAnalysis,
        )
        analysis = ArtifactAnalysis(
            summary="Test activity",
            business_logic="Processes payment",
            data_reads=["Amount"],
            data_writes=["NetAmount"],
            external_calls=["REST: PaymentService"],
            side_effects=["sends email"],
            helix_mapping={"name": "process_payment"},
        )
        code = _heuristic_generate_code(analysis, "activity")
        assert "async def process_payment" in code
        assert "Amount" in code
        assert "NetAmount" in code


class TestAnalyzeAPI:
    async def test_status(self, client):
        resp = await client.get("/api/v1/scout-ai/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "ollama_available" in data
        assert "features" in data

    async def test_analyze_pega_code(self, client):
        resp = await client.post("/api/v1/scout-ai/analyze", json={
            "code": SAMPLE_PEGA_CODE,
            "artifact_type": "activity",
            "source_platform": "pega",
            "identifier": "CalculatePayout",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["identifier"] == "CalculatePayout"
        assert data["complexity"] in ("low", "medium", "high", "extreme")
        assert data["source"] in ("llm", "heuristic")

    async def test_analyze_and_save(self, client):
        resp = await client.post("/api/v1/scout-ai/analyze", json={
            "code": SAMPLE_PEGA_CODE,
            "artifact_type": "activity",
            "source_platform": "pega",
            "identifier": "SaveTest",
            "save": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["saved_id"] is not None

        get_resp = await client.get(f"/api/v1/scout-ai/analyses/{data['saved_id']}")
        assert get_resp.status_code == 200

    async def test_analyze_empty_code_rejected(self, client):
        resp = await client.post("/api/v1/scout-ai/analyze", json={
            "code": "",
            "artifact_type": "activity",
            "source_platform": "pega",
        })
        assert resp.status_code == 422

    async def test_list_analyses(self, client):
        for i in range(2):
            await client.post("/api/v1/scout-ai/analyze", json={
                "code": f"// Code {i}\n" + SAMPLE_PEGA_CODE,
                "identifier": f"Analysis{i}", "save": True,
            })

        resp = await client.get("/api/v1/scout-ai/analyses")
        assert resp.status_code == 200
        assert len(resp.json()) >= 2

    async def test_generate_code_endpoint(self, client):
        resp = await client.post("/api/v1/scout-ai/generate-code", json={
            "code": SAMPLE_PEGA_CODE,
            "artifact_type": "activity",
            "source_platform": "pega",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "generated_code" in data
        assert len(data["generated_code"]) > 0

    async def test_delete_analysis(self, client):
        create = await client.post("/api/v1/scout-ai/analyze", json={
            "code": SAMPLE_PEGA_CODE,
            "identifier": "ToDelete", "save": True,
        })
        aid = create.json()["saved_id"]

        del_resp = await client.delete(f"/api/v1/scout-ai/analyses/{aid}")
        assert del_resp.status_code == 204

        get_resp = await client.get(f"/api/v1/scout-ai/analyses/{aid}")
        assert get_resp.status_code == 404

    async def test_analysis_not_found(self, client):
        resp = await client.get(f"/api/v1/scout-ai/analyses/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestIntegrationPatterns:
    def test_detects_pega_rest_call(self):
        from case_service.scout.ai.analyzer import _heuristic_analyze
        code = 'ConnectREST conn = tools.getConnectRest("PaymentServiceCall");'
        result = _heuristic_analyze(code, "activity", "pega")
        assert any("PaymentServiceCall" in c or "REST" in c for c in result.external_calls)

    def test_detects_http_api_calls(self):
        from case_service.scout.ai.analyzer import _heuristic_analyze
        code = 'const response = await fetch("/api/users/123");'
        result = _heuristic_analyze(code, "script", "camunda")
        assert len(result.external_calls) > 0

    def test_detects_side_effects(self):
        from case_service.scout.ai.analyzer import _heuristic_analyze
        code = """
        sendEmail("user@example.com", "Welcome");
        logAudit("User registered");
        createFile("/tmp/report.pdf");
        """
        result = _heuristic_analyze(code, "activity", "pega")
        effects_str = " ".join(result.side_effects).lower()
        assert "email" in effects_str
        assert "audit" in effects_str
        assert "file" in effects_str
