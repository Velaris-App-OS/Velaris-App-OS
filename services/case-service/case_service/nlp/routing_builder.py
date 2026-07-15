"""HxDraft P3 — natural language → a DRAFT routing change (one step's assignment).

Deliberately surgical: the LLM proposes ONLY ``{stage_id, step_id, assignment}``
and the SERVER patches that one step in the case-type definition — a routing
change never lets the model rewrite the whole definition.

Gate:
  * ``strategy`` (and ``fallback_strategy`` if present) must exist in the
    production assignment registry ``assignment_router._RESOLVERS`` — the same
    closed-set-from-the-engine pattern as P1's operators-from-``_OPS``;
  * the stage and step must exist on the case type;
  * schema-strict — unknown keys never survive.

No fallback: misrouted work is silently lost work. AI down → RoutingDraftError.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from case_service.core.assignment_router import _RESOLVERS
from case_service.hxnexus.factory import generate_json as _ai_generate_json

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000

ROUTING_PROMPT = """You are an expert in work routing for business processes.
You will receive the case type's stages/steps and an instruction. Pick the ONE step
the instruction refers to and output its new assignment as JSON. Output ONLY valid
JSON, no markdown fences, no commentary.

JSON structure (use EXACTLY these keys):
{
  "stage_id": "<stage id from the structure>",
  "step_id": "<step id from the structure>",
  "assignment": {
    "strategy": "specific_user|role_based|queue_based|round_robin|least_loaded|self_service|rule_based|manager_of|skill_based",
    "target": "<user id / role / queue name / skill when the strategy needs one, else null>",
    "fallback_strategy": "<a strategy to try if the primary finds no one, or null>"
  }
}"""


class RoutingDraftError(Exception):
    """Draft generation failed in a way the card should state honestly."""


# ── validation gate ─────────────────────────────────────────────────────────────

def validate_routing_draft(draft: dict[str, Any],
                           case_type_definition: dict[str, Any]) -> list[str]:
    """Every reason this draft must not become a card. Empty list = renderable."""
    errors: list[str] = []
    if not isinstance(draft, dict):
        return ["Draft is not an object"]

    stages = {s.get("id"): s for s in (case_type_definition or {}).get("stages", [])
              if isinstance(s, dict)}
    stage = stages.get(draft.get("stage_id"))
    if stage is None:
        errors.append(f"stage {draft.get('stage_id')!r} does not exist on this "
                      f"case type")
    else:
        steps = {st.get("id") for st in stage.get("steps", []) if isinstance(st, dict)}
        if draft.get("step_id") not in steps:
            errors.append(f"step {draft.get('step_id')!r} does not exist in stage "
                          f"{draft.get('stage_id')!r}")

    assignment = draft.get("assignment")
    if not isinstance(assignment, dict):
        return errors + ["Draft has no assignment"]
    strategy = assignment.get("strategy")
    if strategy not in _RESOLVERS:
        errors.append(f"strategy {strategy!r} is not in the assignment engine's "
                      f"registry {sorted(_RESOLVERS)}")
    fallback = assignment.get("fallback_strategy")
    if fallback is not None and fallback not in _RESOLVERS:
        errors.append(f"fallback_strategy {fallback!r} is not in the assignment "
                      f"engine's registry")
    if strategy in ("specific_user", "role_based", "queue_based", "manager_of",
                    "skill_based") and not str(assignment.get("target") or "").strip():
        errors.append(f"strategy {strategy!r} needs a target")
    return errors


# ── normalization (schema-strict: unknown keys never survive) ───────────────────

def normalize_routing_draft(raw: dict[str, Any]) -> dict[str, Any]:
    assignment_raw = raw.get("assignment") or {}
    assignment: dict[str, Any] = {
        "strategy": str(assignment_raw.get("strategy") or "").strip()}
    if assignment_raw.get("target") is not None:
        assignment["target"] = str(assignment_raw["target"]).strip()[:255]
    if assignment_raw.get("fallback_strategy"):
        assignment["fallback_strategy"] = str(
            assignment_raw["fallback_strategy"]).strip()
    return {
        "stage_id": str(raw.get("stage_id") or "").strip()[:255],
        "step_id": str(raw.get("step_id") or "").strip()[:255],
        "assignment": assignment,
    }


def patch_step_assignment(definition: dict[str, Any],
                          draft: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the definition with ONLY the drafted step's assignment
    replaced — the server-side surgical patch (never an LLM rewrite)."""
    import copy
    patched = copy.deepcopy(definition or {})
    for stage in patched.get("stages", []):
        if isinstance(stage, dict) and stage.get("id") == draft["stage_id"]:
            for step in stage.get("steps", []):
                if isinstance(step, dict) and step.get("id") == draft["step_id"]:
                    step["assignment"] = draft["assignment"]
                    return patched
    raise ValueError("stage/step not found")   # validate gate runs first


# ── generation ──────────────────────────────────────────────────────────────────

def _structure_view(definition: dict[str, Any]) -> dict[str, Any]:
    """The MINIMAL structure the model needs to pick a step — ids and names only."""
    return {"stages": [
        {"id": s.get("id"), "name": s.get("name"),
         "steps": [{"id": st.get("id"), "name": st.get("name"),
                    "assignment": st.get("assignment")}
                   for st in s.get("steps", []) if isinstance(st, dict)]}
        for s in (definition or {}).get("stages", []) if isinstance(s, dict)]}


async def generate_routing_draft(description: str,
                                 case_type_definition: dict[str, Any]
                                 ) -> dict[str, Any]:
    """NL + case-type structure → normalized surgical routing draft + gate errors."""
    import json
    description = (description or "").strip()
    if not description:
        raise RoutingDraftError("Describe the routing change you want drafted")
    if len(description) > MAX_PROMPT_CHARS:
        raise RoutingDraftError(f"Description too long (max {MAX_PROMPT_CHARS} "
                                f"characters)")

    prompt = (f"CASE TYPE STRUCTURE (JSON):\n"
              f"{json.dumps(_structure_view(case_type_definition))[:8000]}\n\n"
              f"INSTRUCTION:\n{description}")
    raw = await _ai_generate_json(prompt=prompt, system=ROUTING_PROMPT)
    if not raw or not isinstance(raw, dict):
        raise RoutingDraftError("AI backend unavailable — routing is never guessed")

    draft = normalize_routing_draft(raw)
    return {"source": "llm", "draft": draft,
            "errors": validate_routing_draft(draft, case_type_definition)}
