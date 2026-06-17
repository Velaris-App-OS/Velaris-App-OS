"""Test Suite (core, #27) — deterministic structural test generation.

Structural tests are derived purely from a case type's `definition_json`
(stages/forms/fields) — no LLM, always correct, executable by the core runner.
This is core (always available); the AI scenario layer lives in
`case_service.hxtest.generator` and is gated by the HxTest marketplace install.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations


def generate_structural(case_type_id: str, definition_json: dict) -> list[dict]:
    """Generate structural tests from a case type's definition_json.

    Always correct: they verify the fetched case type matches its declaration
    (stage count + each stage present). Executable by the core runner."""
    stages = (definition_json or {}).get("stages", [])
    asserts: list[dict] = [
        {"path": "response_status", "op": "status", "value": 200},
        {"path": "response.definition_json.stages", "op": "len", "value": len(stages)},
    ]
    for s in stages:
        name = s.get("name") or s.get("id")
        if name:
            asserts.append({"path": "response.definition_json.stages", "op": "has", "value": name})
    return [{
        "id": f"structural-{case_type_id}",
        "name": "structural: case type matches its definition",
        "suite": "generated", "tags": ["generated", "structural"],
        "rationale": "Deterministic structural check generated from definition_json "
                     "(stage count + each declared stage is present).",
        "generated_by": "structural",
        "steps": [{"action": "api_get", "endpoint": f"/api/v1/case-types/{case_type_id}",
                   "assert": asserts}],
    }]
