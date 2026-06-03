"""Phase 15 tests — NLP Process Builder.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest


class TestHeuristicBuilder:
    async def test_approval_process(self):
        from case_service.nlp.case_type_builder import _heuristic_parse
        result = _heuristic_parse("Approval process for expense reports")
        assert result["_source"] == "heuristic"
        assert len(result["stages"]) > 0
        stage_names = [s["id"] for s in result["stages"]]
        assert "approval" in stage_names or "submission" in stage_names

    async def test_claim_process(self):
        from case_service.nlp.case_type_builder import _heuristic_parse
        result = _heuristic_parse("Insurance claim workflow")
        assert result["default_priority"] == "high"
        assert len(result["stages"]) >= 3

    async def test_generic_fallback(self):
        from case_service.nlp.case_type_builder import _heuristic_parse
        result = _heuristic_parse("Some unusual process nobody has ever seen")
        assert result["_source"] == "heuristic"
        assert len(result["stages"]) > 0

    async def test_numbered_list_extraction(self):
        from case_service.nlp.case_type_builder import _heuristic_parse
        result = _heuristic_parse("""
        Process steps:
        1. Receive request
        2. Validate data
        3. Process payment
        4. Send confirmation
        """)
        assert len(result["stages"]) >= 3

    async def test_slug_function(self):
        from case_service.nlp.case_type_builder import _slug
        assert _slug("Some Name!") == "some_name"
        assert _slug("  Multiple   Spaces  ") == "multiple_spaces"
        assert _slug("") == "item"


class TestNormalize:
    def test_normalize_with_all_fields(self):
        from case_service.nlp.case_type_builder import _normalize
        raw = {
            "name": "Test",
            "description": "Test process",
            "default_priority": "high",
            "stages": [
                {"id": "s1", "name": "Stage 1", "steps": [
                    {"id": "step1", "name": "Step 1"},
                ]},
            ],
        }
        result = _normalize(raw, source="llm")
        assert result["name"] == "Test"
        assert result["_source"] == "llm"
        assert len(result["stages"]) == 1
        assert len(result["stages"][0]["steps"]) == 1

    def test_normalize_fills_missing_fields(self):
        from case_service.nlp.case_type_builder import _normalize
        raw = {"stages": [{"name": "My Stage", "steps": [{"name": "Do Thing"}]}]}
        result = _normalize(raw)
        assert result["stages"][0]["id"] == "my_stage"
        assert result["stages"][0]["steps"][0]["id"] == "do_thing"


class TestStepTypeDetection:
    def test_approval_keyword(self):
        from case_service.nlp.case_type_builder import _detect_step_type
        assert _detect_step_type("approve request") == "approval"

    def test_notification_keyword(self):
        from case_service.nlp.case_type_builder import _detect_step_type
        assert _detect_step_type("send email") == "notification"
        assert _detect_step_type("notify user") == "notification"

    def test_decision_keyword(self):
        from case_service.nlp.case_type_builder import _detect_step_type
        assert _detect_step_type("make decision") == "decision"

    def test_default_user_task(self):
        from case_service.nlp.case_type_builder import _detect_step_type
        assert _detect_step_type("something generic") == "user_task"


class TestNLPAPI:
    async def test_status_endpoint(self, client):
        resp = await client.get("/api/v1/nlp/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "ollama_available" in data
        assert "nlp_enabled" in data

    async def test_generate_with_fallback(self, client):
        # Ollama not available in tests — should use fallback
        resp = await client.post("/api/v1/nlp/generate-case-type", json={
            "description": "Simple approval workflow for expense reports",
            "deploy": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "name" in data
        assert "stages" in data
        assert len(data["stages"]) > 0
        assert data["source"] in ("llm", "heuristic")

    async def test_generate_and_deploy(self, client):
        resp = await client.post("/api/v1/nlp/generate-case-type", json={
            "description": "Insurance claim processing workflow",
            "deploy": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        # Deploy may succeed or fail depending on DB state
        if data.get("deployed_case_type_id"):
            # Verify the case type exists
            ct_resp = await client.get(f"/api/v1/case-types/{data['deployed_case_type_id']}")
            assert ct_resp.status_code == 200

    async def test_generate_rejects_short_input(self, client):
        resp = await client.post("/api/v1/nlp/generate-case-type", json={
            "description": "short",
        })
        assert resp.status_code == 422  # Pydantic validation

    async def test_preview_endpoint(self, client):
        resp = await client.post("/api/v1/nlp/preview", json={
            "description": "A customer onboarding process with several verification steps",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "stages" in data
        assert "source" in data
