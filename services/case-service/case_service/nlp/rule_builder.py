"""HxDraft P1 — natural language → a DRAFT WHEN-rule, validated before anyone sees it.

The generator produces a draft; the validation gate decides whether it may even be
rendered as a card. The gate is the security boundary (design §6):

  * **Closed action set** — only {set_value, auto_approve, advance_stage, skip_stage,
    send_notification, assign_to, log}. Nothing that touches security, authz,
    credentials, namespaces, or grants is expressible.
  * ``set_value`` targets must look like case-variable paths and never hit the
    forbidden prefixes (security/credential/grant/… — deny by substring, fail closed).
  * Operators must exist in the production rules engine's registry.
  * Any ``expression`` must be safe_expression CONFORMING (the hardened eval fallback
    is unreachable from drafts).
  * Bounded: ≤20 conditions, ≤10 actions, prompt ≤4000 chars — a human must be able
    to actually review the card.

No heuristic fallback: a guessed rule is a wrong rule. AI unavailable → RuleDraftError.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from case_service.core.rules_evaluator import _OPS
from case_service.hxnexus.factory import generate_json as _ai_generate_json
from case_service.nlp.case_type_builder import _slug

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000
MAX_CONDITIONS = 20
MAX_ACTIONS = 10

#: the ONLY action types a draft may carry (mirrors HxEvolve's closed set)
CLOSED_ACTION_TYPES = {
    "set_value", "auto_approve", "advance_stage", "skip_stage",
    "send_notification", "assign_to", "log",
}

#: a set_value target containing any of these is rejected outright (fail closed)
FORBIDDEN_TARGET_PARTS = (
    "security", "authz", "credential", "password", "secret", "token",
    "grant", "namespace", "role", "permission", "privilege", "tenant",
)

RULE_PROMPT = """You are an expert in business-process automation rules.
Convert the user's description into ONE rule as JSON. Output ONLY valid JSON,
no markdown fences, no commentary.

JSON structure (use EXACTLY these keys):
{
  "name": "<short descriptive name>",
  "description": "<one sentence>",
  "conditions": [
    {"field_path": "<dotted variable path, e.g. claim.amount>",
     "operator": "eq|neq|gt|gte|lt|lte|in|not_in|contains|starts_with|ends_with|is_empty|is_not_empty|between",
     "value": <JSON value>}
  ],
  "actions": [
    {"action_type": "set_value|auto_approve|advance_stage|skip_stage|send_notification|assign_to|log",
     "target": "<path or stage id or recipient, when applicable>",
     "value": <JSON value or null>}
  ]
}
Conditions are ANDed. Prefer few precise conditions over many vague ones.
Never invent actions outside the allowed list."""


class RuleDraftError(Exception):
    """Draft generation failed in a way the card should state honestly."""


# ── validation gate ─────────────────────────────────────────────────────────────

def validate_rule_draft(draft: dict[str, Any]) -> list[str]:
    """Every reason this draft must not become a card. Empty list = renderable."""
    errors: list[str] = []
    if not isinstance(draft, dict):
        return ["Draft is not an object"]

    d = draft.get("definition_json")
    if not isinstance(d, dict):
        return ["Draft has no definition_json"]

    if draft.get("rule_type") != "when":
        errors.append(f"Only WHEN rules are draftable in P1 (got {draft.get('rule_type')!r})")

    if not str(draft.get("name") or "").strip():
        errors.append("Rule needs a name")

    conditions = d.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        errors.append("Rule needs at least one condition")
        conditions = []
    if len(conditions) > MAX_CONDITIONS:
        errors.append(f"Too many conditions ({len(conditions)} > {MAX_CONDITIONS})")
    for i, c in enumerate(conditions):
        if not isinstance(c, dict) or not str(c.get("field_path") or "").strip():
            errors.append(f"Condition {i + 1}: needs a field_path")
            continue
        op = c.get("operator", "eq")
        if op not in _OPS:
            errors.append(f"Condition {i + 1}: unknown operator {op!r}")

    actions = d.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("Rule needs at least one action")
        actions = []
    if len(actions) > MAX_ACTIONS:
        errors.append(f"Too many actions ({len(actions)} > {MAX_ACTIONS})")
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            errors.append(f"Action {i + 1}: not an object")
            continue
        at = a.get("action_type")
        if at not in CLOSED_ACTION_TYPES:
            errors.append(f"Action {i + 1}: {at!r} is outside the closed action set")
            continue
        if at == "set_value":
            errors.extend(_set_value_target_errors(i, a))

    # an expression anywhere must parse within the strict safe grammar
    expression = d.get("expression")
    if expression:
        from case_service.core.safe_expression import Classification, classify_expression
        cls, reason = classify_expression(str(expression))
        if cls is not Classification.CONFORMING:
            errors.append(f"Expression rejected by HxSandbox: {reason or cls.value}")

    return errors


def _set_value_target_errors(i: int, action: dict[str, Any]) -> list[str]:
    target = str(action.get("target") or "").strip()
    if not target:
        return [f"Action {i + 1}: set_value needs a target"]
    low = target.lower()
    for part in FORBIDDEN_TARGET_PARTS:
        if part in low:
            return [f"Action {i + 1}: set_value target {target!r} touches a forbidden "
                    f"surface ({part!r})"]
    # variable-ish dotted path or case.data.* only — never bare system fields
    bare = low[len("case.data."):] if low.startswith("case.data.") else low
    if bare.startswith("case."):
        return [f"Action {i + 1}: set_value may only target case variables, "
                f"not {target!r}"]
    return []


# ── normalization (schema-strict: unknown keys never survive) ───────────────────

def normalize_rule_draft(raw: dict[str, Any], *, scope_target_id: str | None = None,
                         prompt: str = "") -> dict[str, Any]:
    name = str(raw.get("name") or "Drafted rule").strip()[:255]
    conditions = [
        {"field_path": str(c.get("field_path") or "").strip()[:512],
         "operator": str(c.get("operator") or "eq"),
         **({"value_field_path": str(c["value_field_path"])[:512]}
            if c.get("value_field_path") else {"value": c.get("value")})}
        for c in (raw.get("conditions") or []) if isinstance(c, dict)
    ][:MAX_CONDITIONS + 1]          # +1 so the validator can still say "too many"
    actions = [
        {"action_type": str(a.get("action_type") or ""),
         "target": (str(a.get("target"))[:512] if a.get("target") is not None else None),
         "value": a.get("value")}
        for a in (raw.get("actions") or []) if isinstance(a, dict)
    ][:MAX_ACTIONS + 1]

    provenance = f'Drafted by HxNexus — "{prompt.strip()[:300]}"' if prompt else "Drafted by HxNexus"
    description = str(raw.get("description") or "").strip()[:500]
    return {
        "name": name,
        "version": "1.0.0",
        "rule_type": "when",
        "scope": "case_type" if scope_target_id else "global",
        "scope_target_id": scope_target_id,
        "enabled": True,
        "priority": 0,
        "definition_json": {"conditions": conditions, "actions": actions},
        "description": f"{description} · {provenance}" if description else provenance,
        "id": _slug(name),          # draft key only; the DB assigns the real id on Apply
    }


# ── generation ──────────────────────────────────────────────────────────────────

async def generate_rule_draft(description: str, *, scope_target_id: str | None = None
                              ) -> dict[str, Any]:
    """NL → normalized draft + its validation errors. Never guesses, never fixes up.

    Returns ``{"draft": {...}, "errors": [...]}`` — the card renders the draft only
    when errors is empty, and states the errors verbatim otherwise.
    """
    description = (description or "").strip()
    if not description:
        raise RuleDraftError("Describe the rule you want drafted")
    if len(description) > MAX_PROMPT_CHARS:
        raise RuleDraftError(f"Description too long (max {MAX_PROMPT_CHARS} characters)")

    raw = await _ai_generate_json(prompt=description, system=RULE_PROMPT)
    if not raw or not isinstance(raw, dict):
        # a heuristic rule guess is a wrong rule — fail honestly instead
        raise RuleDraftError("AI backend unavailable — rules cannot be drafted heuristically")

    draft = normalize_rule_draft(raw, scope_target_id=scope_target_id, prompt=description)
    return {"draft": draft, "errors": validate_rule_draft(draft)}
