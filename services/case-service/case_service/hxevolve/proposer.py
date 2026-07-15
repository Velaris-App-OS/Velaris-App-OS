"""HxEvolve §3.2 Propose — AI on rails.

The proposer turns one mining candidate into AT MOST one config mutation from the
five signed-off kinds, then RE-VALIDATES it through the same gates HxDraft ships
(rules / SLA / routing) or the permutation-only reorder gate. A proposal that
fails its gate is returned with errors so the pipeline records a
``discarded_gate`` insight — it never reaches a human as a suggestion.

Prompt-injection posture (§6): the prompt contains mining STATISTICS and the
current (trusted) config shape only — never raw case content, never user
free-text. AI output is data: schema-strict extraction, unknown keys dropped,
no fallback (a guessed optimization is a wrong optimization).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.hxnexus.factory import generate_json as _ai_generate_json
from case_service.hxevolve import gates
from case_service.nlp import routing_builder, rule_builder, sla_builder

logger = logging.getLogger(__name__)

PROPOSAL_KINDS = ("rule_adjust", "rule_add", "sla_duration", "routing", "reorder")

PROPOSER_PROMPT = """You are an expert in business-process optimization.
You will receive: an OPTIMIZATION SIGNAL (mining statistics), and the CURRENT
CONFIG of a case type (stages/steps with assignments, SLA policies, WHEN rules).

Propose EXACTLY ONE minimal, reversible config change that addresses the signal.
Output ONLY valid JSON, no markdown fences, using EXACTLY this structure:

{
  "kind": "rule_adjust|rule_add|sla_duration|routing|reorder",
  "rationale": "<one or two sentences: why this change addresses the signal>",
  "policy_alternative": "<a non-automation alternative a manager could choose>",
  "rule_id": "<for rule_adjust: the id of the rule to change, else null>",
  "rule": {"name": "<for rule_add>", "conditions": [{"field_path": "...", "operator": "eq|neq|gt|gte|lt|lte|in|not_in|contains", "value": <JSON>}], "actions": [{"action_type": "set_value|auto_approve|advance_stage|skip_stage|send_notification|assign_to|log", "target": "<or null>", "value": <JSON or null>}]},
  "sla": {"policy_id": "<for sla_duration>", "goal_duration": "<ISO 8601>", "deadline_duration": "<ISO 8601>"},
  "routing": {"stage_id": "...", "step_id": "...", "assignment": {"strategy": "specific_user|role_based|queue_based|round_robin|least_loaded|self_service|rule_based|manager_of|skill_based", "target": "<or null>", "fallback_strategy": "<or null>"}},
  "reorder": {"stages": [<the COMPLETE stages array with steps moved/parallelized — never add, remove or edit a step>]}

Fill ONLY the section for your chosen kind; set the others to null.
Never touch security, permissions, credentials or data retention. Prefer the
smallest change that plausibly helps."""


class ProposeError(Exception):
    """Proposal generation failed in a way the pipeline should record honestly."""


def _config_view(case_type, rules: list) -> dict[str, Any]:
    """The TRUSTED config shape the model may see — no case data, no PII."""
    definition = case_type.definition_json or {}
    return {
        "case_type": case_type.name,
        "stages": [
            {"id": s.get("id"), "name": s.get("name"),
             "stage_type": s.get("stage_type", "linear"),
             "steps": [{"id": st.get("id"), "name": st.get("name"),
                        "step_type": st.get("step_type"),
                        "assignment": st.get("assignment")}
                       for st in s.get("steps", []) if isinstance(st, dict)]}
            for s in definition.get("stages", []) if isinstance(s, dict)],
        "sla_policies": definition.get("sla_policies", []),
        "when_rules": [
            {"id": str(r.id), "name": r.name, "enabled": r.enabled,
             "conditions": (r.definition_json or {}).get("conditions", []),
             "actions": (r.definition_json or {}).get("actions", [])}
            for r in rules],
    }


async def propose(session: AsyncSession, candidate: dict[str, Any],
                  case_type, rules: list) -> dict[str, Any]:
    """One candidate → one gated proposal.

    Returns ``{kind, proposal, rationale, policy_alternative, errors}`` —
    non-empty errors means the pipeline records it as ``discarded_gate``.
    """
    prompt = (f"OPTIMIZATION SIGNAL (mining statistics):\n"
              f"{json.dumps(candidate, default=str)[:4000]}\n\n"
              f"CURRENT CONFIG:\n"
              f"{json.dumps(_config_view(case_type, rules), default=str)[:10000]}")
    raw = await _ai_generate_json(prompt=prompt, system=PROPOSER_PROMPT)
    if not raw or not isinstance(raw, dict):
        raise ProposeError("AI backend unavailable — optimizations are never guessed")

    kind = raw.get("kind")
    rationale = str(raw.get("rationale") or "").strip()[:1000]
    policy_alternative = str(raw.get("policy_alternative") or "").strip()[:1000]
    if kind not in PROPOSAL_KINDS:
        return {"kind": kind, "proposal": {}, "rationale": rationale,
                "policy_alternative": policy_alternative,
                "errors": [f"kind {kind!r} is outside the closed proposal "
                           f"vocabulary {list(PROPOSAL_KINDS)}"]}

    definition = case_type.definition_json or {}
    build = {
        "rule_adjust": _build_rule_adjust,
        "rule_add": _build_rule_add,
        "sla_duration": _build_sla_duration,
        "routing": _build_routing,
        "reorder": _build_reorder,
    }[kind]
    proposal, errors = build(raw, case_type=case_type, definition=definition,
                             rules=rules)
    return {"kind": kind, "proposal": proposal, "rationale": rationale,
            "policy_alternative": policy_alternative, "errors": errors}


# ── per-kind builders: normalize schema-strict, then THE gate ────────────────────

def _build_rule_add(raw, *, case_type, definition, rules):
    payload = raw.get("rule") or {}
    clean = rule_builder.normalize_rule_draft(
        {"name": payload.get("name"), "conditions": payload.get("conditions"),
         "actions": payload.get("actions")},
        scope_target_id=str(case_type.id))
    errors = rule_builder.validate_rule_draft(clean)
    if any((r.name or "") == clean["name"] for r in rules):
        errors.append(f"A rule named {clean['name']!r} already exists on this "
                      f"case type")
    return clean, errors


def _build_rule_adjust(raw, *, case_type, definition, rules):
    payload = raw.get("rule") or {}
    rule_id = str(raw.get("rule_id") or "")
    current = next((r for r in rules if str(r.id) == rule_id), None)
    if current is None:
        return {}, [f"rule_id {rule_id!r} is not a WHEN rule of this case type"]
    clean = rule_builder.normalize_rule_draft(
        {"name": current.name,                       # name immutable, like HxDraft
         "conditions": payload.get("conditions"),
         "actions": payload.get("actions")},
        scope_target_id=current.scope_target_id)
    clean["id"] = str(current.id)   # replay registers a MODIFICATION, not an addition
    errors = rule_builder.validate_rule_draft(clean)
    return clean, errors


def _build_sla_duration(raw, *, case_type, definition, rules):
    payload = raw.get("sla") or {}
    policies = [p for p in definition.get("sla_policies", []) if isinstance(p, dict)]
    pid = str(payload.get("policy_id") or "")
    current = next((p for p in policies if p.get("id") == pid), None)
    if current is None:
        return {}, [f"policy_id {pid!r} is not an SLA policy of this case type"]
    clean = sla_builder.normalize_sla_draft({
        "name": current.get("name"),                 # keep identity → replace-by-id
        "scope": current.get("scope", "case"),
        "target_stage": current.get("target_stage"),
        "goal_duration": payload.get("goal_duration"),
        "deadline_duration": payload.get("deadline_duration"),
    })
    clean["id"] = pid
    remainder = [p for p in policies if p.get("id") != pid]
    errors = sla_builder.validate_sla_draft(
        {"policy": clean}, {**definition, "sla_policies": remainder})
    return {"policy": clean, "replaces_policy_id": pid,
            "before_policy": current}, errors


def _build_routing(raw, *, case_type, definition, rules):
    clean = routing_builder.normalize_routing_draft(raw.get("routing") or {})
    errors = routing_builder.validate_routing_draft(clean, definition)
    return clean, errors


def _build_reorder(raw, *, case_type, definition, rules):
    stages = (raw.get("reorder") or {}).get("stages")
    proposed = {**definition, "stages": stages if isinstance(stages, list) else []}
    errors = gates.validate_reorder(definition, proposed)
    return {"definition_json": proposed}, errors
