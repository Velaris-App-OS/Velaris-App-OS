"""HxDraft P3 — natural language → a DRAFT escalation tree, validated before the card.

An escalation pages people and reassigns work, so the gate is closed on every
axis the engine actually executes:

  * **Closed trigger set** — {goal_pct, deadline_pct, fixed_duration, at_breach}
    (the same pattern `POST /escalation-trees` enforces); percentage triggers must
    be in (0, 200], fixed_duration must parse as an ISO-8601 duration.
  * **Closed action set** — {notify, reassign, priority, status}; notify/reassign
    targets restricted to the engine's resolvable target types
    {current_assignee, manager_of_current_assignee, access_group, role, queue, user}.
  * Bounded and ordered: ≤10 levels, ≤5 actions per level, level numbers strictly
    increasing — a card a human can actually review.
  * Schema-strict normalization — unknown keys never survive.

No fallback: an invented escalation pages the wrong person at the wrong time.
AI unavailable → EscalationDraftError.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from case_service.core.sla_tracker import parse_iso8601_duration
from case_service.hxnexus.factory import generate_json as _ai_generate_json
from case_service.nlp.sla_builder import to_iso8601

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000
MAX_LEVELS = 10
MAX_ACTIONS_PER_LEVEL = 5

#: mirrors the EscalationTrigger schema pattern on the manual endpoint
TRIGGER_TYPES = {"goal_pct", "deadline_pct", "fixed_duration", "at_breach"}
#: mirrors the EscalationAction schema pattern on the manual endpoint
ACTION_TYPES = {"notify", "reassign", "priority", "status"}
#: the engine's resolvable targets (core/sla_escalation.resolve_target)
TARGET_TYPES = {"current_assignee", "manager_of_current_assignee",
                "access_group", "role", "queue", "user"}
PRIORITY_VALUES = {"low", "medium", "high", "critical"}

ESCALATION_PROMPT = """You are an expert in SLA escalation design for business processes.
Convert the user's description into ONE escalation tree as JSON. Output ONLY valid
JSON, no markdown fences, no commentary.

JSON structure (use EXACTLY these keys):
{
  "name": "<short descriptive name>",
  "description": "<one sentence>",
  "levels": [
    {"level": 1, "name": "<level name>",
     "trigger": {"type": "goal_pct|deadline_pct|fixed_duration|at_breach",
                 "value": <percent number, ISO 8601 duration string, or null for at_breach>},
     "actions": [
       {"type": "notify|reassign|priority|status",
        "target_type": "current_assignee|manager_of_current_assignee|access_group|role|queue|user or null",
        "target_id": "<id when the target type needs one, else null>",
        "message": "<notify message or null>",
        "set": "<priority level or status value for priority/status actions, else null>"}
     ]}
  ]
}
Levels fire in order; use few levels with clear triggers."""


class EscalationDraftError(Exception):
    """Draft generation failed in a way the card should state honestly."""


# ── validation gate ─────────────────────────────────────────────────────────────

def validate_escalation_draft(draft: dict[str, Any]) -> list[str]:
    """Every reason this draft must not become a card. Empty list = renderable."""
    errors: list[str] = []
    if not isinstance(draft, dict):
        return ["Draft is not an object"]
    if not str(draft.get("name") or "").strip():
        errors.append("Escalation tree needs a name")

    levels = (draft.get("tree_json") or {}).get("levels")
    if not isinstance(levels, list) or not levels:
        return errors + ["Escalation tree needs at least one level"]
    if len(levels) > MAX_LEVELS:
        errors.append(f"Too many levels ({len(levels)} > {MAX_LEVELS})")

    prev_level = 0
    for i, lvl in enumerate(levels):
        tag = f"Level {i + 1}"
        if not isinstance(lvl, dict):
            errors.append(f"{tag}: not an object")
            continue
        n = lvl.get("level")
        if not isinstance(n, int) or n < 1 or n > 99:
            errors.append(f"{tag}: level number must be an integer 1-99")
        elif n <= prev_level:
            errors.append(f"{tag}: level numbers must be strictly increasing")
        else:
            prev_level = n

        trigger = lvl.get("trigger") or {}
        ttype = trigger.get("type")
        tval = trigger.get("value")
        if ttype not in TRIGGER_TYPES:
            errors.append(f"{tag}: trigger {ttype!r} is outside the closed trigger set")
        elif ttype in ("goal_pct", "deadline_pct"):
            if not isinstance(tval, (int, float)) or not (0 < tval <= 200):
                errors.append(f"{tag}: {ttype} needs a percentage in (0, 200]")
        elif ttype == "fixed_duration":
            try:
                parse_iso8601_duration(str(tval or ""))
            except ValueError:
                errors.append(f"{tag}: fixed_duration {tval!r} is not a valid "
                              f"ISO 8601 duration")

        actions = lvl.get("actions")
        if not isinstance(actions, list) or not actions:
            errors.append(f"{tag}: needs at least one action")
            continue
        if len(actions) > MAX_ACTIONS_PER_LEVEL:
            errors.append(f"{tag}: too many actions ({len(actions)} > "
                          f"{MAX_ACTIONS_PER_LEVEL})")
        for j, action in enumerate(actions):
            atag = f"{tag} action {j + 1}"
            atype = action.get("type") if isinstance(action, dict) else None
            if atype not in ACTION_TYPES:
                errors.append(f"{atag}: {atype!r} is outside the closed action set")
                continue
            if atype in ("notify", "reassign"):
                tt = action.get("target_type")
                if tt not in TARGET_TYPES:
                    errors.append(f"{atag}: target_type {tt!r} is not resolvable "
                                  f"by the escalation engine")
                elif tt in ("access_group", "role", "queue", "user") \
                        and not str(action.get("target_id") or "").strip():
                    errors.append(f"{atag}: target_type {tt!r} needs a target_id")
            if atype == "priority" and action.get("set") not in PRIORITY_VALUES:
                errors.append(f"{atag}: priority must set one of "
                              f"{sorted(PRIORITY_VALUES)}")
            if atype == "status" and not str(action.get("set") or "").strip():
                errors.append(f"{atag}: status action needs a 'set' value")
    return errors


# ── normalization (schema-strict: unknown keys never survive) ───────────────────

def _normalize_action(raw: dict[str, Any]) -> dict[str, Any]:
    action: dict[str, Any] = {"type": str(raw.get("type") or "")}
    if raw.get("target_type") is not None:
        action["target_type"] = str(raw["target_type"]).strip().lower()[:64]
    if raw.get("target_id") is not None:
        action["target_id"] = str(raw["target_id"]).strip()[:255]
    if raw.get("message"):
        action["message"] = str(raw["message"]).strip()[:500]
    if raw.get("set") is not None:
        action["set"] = str(raw["set"]).strip().lower()[:64]
    return action


def normalize_escalation_draft(raw: dict[str, Any], *, case_type_id: str | None = None,
                               prompt: str = "") -> dict[str, Any]:
    name = str(raw.get("name") or "Drafted escalation").strip()[:255]
    provenance = (f'Drafted by HxNexus — "{prompt.strip()[:300]}"'
                  if prompt else "Drafted by HxNexus")
    description = str(raw.get("description") or "").strip()[:500]

    levels = []
    for lvl in (raw.get("levels") or (raw.get("tree_json") or {}).get("levels")
                or [])[:MAX_LEVELS + 1]:
        if not isinstance(lvl, dict):
            continue
        trigger = lvl.get("trigger") or {}
        tval = trigger.get("value")
        if trigger.get("type") == "fixed_duration":
            tval = to_iso8601(tval) or tval          # accept "4h" from the LLM
        levels.append({
            "level": lvl.get("level"),
            "name": str(lvl.get("name") or f"Level {lvl.get('level')}").strip()[:255],
            "trigger": {"type": str(trigger.get("type") or ""), "value": tval},
            "actions": [_normalize_action(a) for a in (lvl.get("actions") or [])
                        if isinstance(a, dict)][:MAX_ACTIONS_PER_LEVEL + 1],
        })

    return {
        "name": name,
        "scope": "case_type" if case_type_id else "global",
        "case_type_id": case_type_id,
        "tree_json": {"levels": levels},
        "description": f"{description} · {provenance}" if description else provenance,
    }


# ── generation ──────────────────────────────────────────────────────────────────

async def generate_escalation_draft(description: str, *,
                                    case_type_id: str | None = None
                                    ) -> dict[str, Any]:
    """NL → normalized escalation draft + validation errors. Never guessed."""
    description = (description or "").strip()
    if not description:
        raise EscalationDraftError("Describe the escalation you want drafted")
    if len(description) > MAX_PROMPT_CHARS:
        raise EscalationDraftError(f"Description too long (max {MAX_PROMPT_CHARS} "
                                   f"characters)")

    raw = await _ai_generate_json(prompt=description, system=ESCALATION_PROMPT)
    if not raw or not isinstance(raw, dict):
        raise EscalationDraftError("AI backend unavailable — escalations are never "
                                   "drafted heuristically")

    draft = normalize_escalation_draft(raw, case_type_id=case_type_id,
                                       prompt=description)
    return {"source": "llm", "draft": draft,
            "errors": validate_escalation_draft(draft)}
