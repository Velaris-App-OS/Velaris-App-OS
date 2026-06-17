"""HxTest (#27) — AI test generation: structural (deterministic) + scenario (HxNexus).

Both emit tests in the core Test Suite's closed DSL so the same runner executes
them. Structural = Layer 1 (no LLM, always correct). Scenario = Layer 2 (HxNexus,
ADVISORY — never blocks the marketplace gate, decision D3).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging

from case_service.testsuite import dsl
# Structural generation is deterministic CORE (Test Suite), not AI — it lives in
# case_service.testsuite.generator. Re-exported here so existing HxTest callers
# (generator.generate_structural) keep working; the core path imports it directly.
from case_service.testsuite.generator import generate_structural  # noqa: F401

logger = logging.getLogger(__name__)


# ── Layer 2 — scenario (HxNexus; advisory) ────────────────────────────────────

_SCENARIO_SYSTEM = (
    "You are a QA engineer for a BPM platform. Given a case type definition, "
    "generate executable test cases (happy path, required-field validation, SLA "
    "breach, rule triggers, portal submission). Each test: id (kebab-case), name, "
    "rationale, steps (Velaris test DSL: api_get/api_post/api_patch/api_delete, "
    "create_case, stage_transition, resolve_case, time_travel). "
    'Return JSON {"tests": [ ... ]}.'
)


async def generate_scenarios(definition_json: dict, *, case_type_name: str = "") -> list[dict]:
    """Generate scenario tests via HxNexus. Returns [] when AI is unavailable
    (advisory layer — never an error) or for any test that fails DSL validation."""
    from case_service.hxnexus.factory import generate_json

    prompt = f"Case type '{case_type_name}' definition:\n{json.dumps(definition_json)}"
    result = await generate_json(prompt, system=_SCENARIO_SYSTEM)
    if not result:
        logger.info("HxTest scenario generation skipped — AI backend unavailable")
        return []

    raw = result.get("tests") if isinstance(result, dict) else result
    if not isinstance(raw, list):
        return []

    valid: list[dict] = []
    for t in raw:
        try:
            dsl.parse_test(t)              # validate before accepting (D4)
        except dsl.DslError as e:
            logger.warning("HxTest discarded invalid generated test: %s", e)
            continue
        t.setdefault("generated_by", "hxnexus")
        t.setdefault("suite", "generated")
        valid.append(t)
    return valid
