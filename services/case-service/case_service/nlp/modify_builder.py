"""HxDraft P2 — modify-existing: "configure the X … " → a DIFF card, never a blind
overwrite.

Every modification draft:
  * receives the CURRENT artifact (trusted config the caller may read — credentials
    are NEVER included) plus the user's instruction, and must output a COMPLETE
    replacement — patch fragments are rejected by normalization;
  * must pass the same gates as a created draft, UNCONDITIONALLY — even if the
    existing artifact contains constructs the gate would reject, a draft can only
    ever *produce* gate-conforming output (fail closed);
  * carries a base checksum of the artifact it was generated from — Apply re-fetches
    and 409s when the target changed in the meantime;
  * has NO fallback of any kind: modifying by guess is wrong twice over. AI down →
    honest ModifyDraftError.

Deliberately NOT re-normalized through the P1 create builders for case types: the
create-path ``_normalize`` keeps only the structural keys and would silently strip
forms/variables/notifications from a live definition. Modify validates the
replacement structurally and lets the DIFF CARD + human review carry the rest.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
from typing import Any

from case_service.core.sla_tracker import parse_iso8601_duration
from case_service.hxnexus.factory import generate_json as _ai_generate_json
from case_service.nlp import form_builder, rule_builder

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000
MAX_DEFINITION_BYTES = 200_000     # a case-type definition a human can still review
MAX_CONFIG_BYTES = 32_000          # connector config bound

#: connector-config keys that must never appear in a draft (creds live in the
#: encrypted credentials field, never in config — smuggling attempts fail closed)
FORBIDDEN_CONFIG_KEY_PARTS = (
    "password", "secret", "token", "credential", "api_key", "apikey",
    "private_key", "passphrase", "auth",
)

_AI_DOWN = "AI backend unavailable — modifications are never guessed"


class ModifyDraftError(Exception):
    """Modification drafting failed in a way the card should state honestly."""


def _current_block(current: dict[str, Any], instruction: str) -> str:
    return (f"CURRENT (JSON):\n{json.dumps(current, default=str)[:12000]}\n\n"
            f"INSTRUCTION:\n{instruction}")


def _check_prompt(description: str) -> str:
    description = (description or "").strip()
    if not description:
        raise ModifyDraftError("Describe the change you want drafted")
    if len(description) > MAX_PROMPT_CHARS:
        raise ModifyDraftError(f"Description too long (max {MAX_PROMPT_CHARS} characters)")
    return description


# ── rules ───────────────────────────────────────────────────────────────────────

RULE_MODIFY_PROMPT = """You are an expert in business-process automation rules.
You will receive the CURRENT rule as JSON and an instruction. Output the COMPLETE
modified rule as JSON — every condition and action that should remain must be
repeated; anything you omit is removed. Output ONLY valid JSON, no markdown fences.

JSON structure (use EXACTLY these keys):
{
  "description": "<one sentence>",
  "conditions": [{"field_path": "...", "operator": "eq|neq|gt|gte|lt|lte|in|not_in|contains|starts_with|ends_with|is_empty|is_not_empty|between", "value": <JSON>}],
  "actions": [{"action_type": "set_value|auto_approve|advance_stage|skip_stage|send_notification|assign_to|log", "target": "<path/stage/recipient or null>", "value": <JSON or null>}]
}
Never invent actions outside the allowed list."""


async def generate_rule_modification(
    description: str, *, current_name: str, current_definition: dict[str, Any],
    scope_target_id: str | None = None,
) -> dict[str, Any]:
    """Instruction + current WHEN rule → normalized full replacement + gate errors.

    The name is immutable in modify mode (parity with the manual rules PATCH,
    which only updates the definition)."""
    description = _check_prompt(description)
    current = {"conditions": current_definition.get("conditions", []),
               "actions": current_definition.get("actions", [])}
    raw = await _ai_generate_json(
        prompt=_current_block(current, description), system=RULE_MODIFY_PROMPT)
    if not raw or not isinstance(raw, dict):
        raise ModifyDraftError(_AI_DOWN)

    raw["name"] = current_name                       # immutable in modify mode
    draft = rule_builder.normalize_rule_draft(
        raw, scope_target_id=scope_target_id, prompt=description)
    # THE gate, unconditional — regardless of what the existing rule contains.
    return {"draft": draft, "errors": rule_builder.validate_rule_draft(draft)}


# ── case types ──────────────────────────────────────────────────────────────────

CASE_TYPE_MODIFY_PROMPT = """You are an expert in business process modeling.
You will receive the CURRENT case-type definition as JSON and an instruction.
Output the COMPLETE modified definition as JSON — repeat every stage, step, form
field, variable, SLA policy and notification that should remain; anything you omit
is removed. Keep all ids stable unless the instruction says otherwise.
Output ONLY valid JSON, no markdown fences."""


def validate_case_type_definition(definition: Any) -> list[str]:
    """Structural gate for a full replacement definition."""
    errors: list[str] = []
    if not isinstance(definition, dict):
        return ["Definition is not an object"]
    if len(json.dumps(definition, default=str)) > MAX_DEFINITION_BYTES:
        errors.append("Modified definition is too large to review")
    stages = definition.get("stages")
    if not isinstance(stages, list) or not stages:
        errors.append("Definition needs a non-empty stages list")
        stages = []
    seen: set[str] = set()
    for i, stage in enumerate(stages):
        if not isinstance(stage, dict) or not stage.get("id"):
            errors.append(f"Stage {i + 1}: needs an id")
            continue
        if stage["id"] in seen:
            errors.append(f"Stage {i + 1}: duplicate id {stage['id']!r}")
        seen.add(stage["id"])
        if not isinstance(stage.get("steps"), list):
            errors.append(f"Stage {stage['id']!r}: steps must be a list")
    for j, policy in enumerate(definition.get("sla_policies", []) or []):
        if not isinstance(policy, dict):
            errors.append(f"SLA policy {j + 1}: not an object")
            continue
        for key in ("goal_duration", "deadline_duration"):
            if key in policy:
                try:
                    parse_iso8601_duration(str(policy[key]))
                except ValueError:
                    errors.append(f"SLA policy {j + 1}: {key} "
                                  f"{policy[key]!r} is not a valid duration")
    return errors


async def generate_case_type_modification(
    description: str, *, current_definition: dict[str, Any],
) -> dict[str, Any]:
    description = _check_prompt(description)
    raw = await _ai_generate_json(
        prompt=_current_block(current_definition, description),
        system=CASE_TYPE_MODIFY_PROMPT)
    if not raw or not isinstance(raw, dict):
        raise ModifyDraftError(_AI_DOWN)
    return {"draft": {"definition_json": raw},
            "errors": validate_case_type_definition(raw)}


# ── forms ───────────────────────────────────────────────────────────────────────

FORM_MODIFY_PROMPT = """You are an expert in form design.
You will receive the CURRENT form fields as JSON and an instruction. Output the
COMPLETE modified form as JSON — repeat every field that should remain; anything
you omit is removed. Keep field ids stable unless the instruction says otherwise.
Output ONLY valid JSON, no markdown fences.

JSON structure (use EXACTLY these keys):
{"fields": [{"id": "<snake_case>", "label": "<label>", "field_type": "text|textarea|number|date|datetime|email|phone|select|multiselect|boolean|file|currency", "required": true|false, "options": [..] or null}]}"""


async def generate_form_modification(
    description: str, *, current_name: str, current_definition: dict[str, Any],
) -> dict[str, Any]:
    description = _check_prompt(description)
    current = {"fields": current_definition.get("fields", [])}
    raw = await _ai_generate_json(
        prompt=_current_block(current, description), system=FORM_MODIFY_PROMPT)
    if not raw or not isinstance(raw, dict):
        raise ModifyDraftError(_AI_DOWN)

    draft = form_builder.normalize_form_draft(
        {"name": current_name, "fields": raw.get("fields")}, prompt=description)
    return {"draft": draft, "errors": form_builder.validate_form_draft(draft)}


# ── escalation trees ────────────────────────────────────────────────────────────

ESCALATION_MODIFY_PROMPT = """You are an expert in SLA escalation design.
You will receive the CURRENT escalation levels as JSON and an instruction. Output
the COMPLETE modified tree as JSON — repeat every level and action that should
remain; anything you omit is removed. Output ONLY valid JSON, no markdown fences.

JSON structure (use EXACTLY these keys):
{"levels": [{"level": <int>, "name": "<name>",
  "trigger": {"type": "goal_pct|deadline_pct|fixed_duration|at_breach", "value": <pct, ISO 8601 duration, or null>},
  "actions": [{"type": "notify|reassign|priority|status", "target_type": "current_assignee|manager_of_current_assignee|access_group|role|queue|user or null", "target_id": "<id or null>", "message": "<or null>", "set": "<or null>"}]}]}"""


async def generate_escalation_modification(
    description: str, *, current_name: str, current_tree: dict[str, Any],
) -> dict[str, Any]:
    """Instruction + current tree → normalized full replacement + gate errors.
    Name/scope immutable in modify mode."""
    from case_service.nlp import escalation_builder

    description = _check_prompt(description)
    current = {"levels": (current_tree or {}).get("levels", [])}
    raw = await _ai_generate_json(
        prompt=_current_block(current, description), system=ESCALATION_MODIFY_PROMPT)
    if not raw or not isinstance(raw, dict):
        raise ModifyDraftError(_AI_DOWN)

    raw["name"] = current_name                        # immutable in modify mode
    draft = escalation_builder.normalize_escalation_draft(raw, prompt=description)
    return {"draft": draft,
            "errors": escalation_builder.validate_escalation_draft(draft)}


# ── connector config (never credentials) ────────────────────────────────────────

CONNECTOR_MODIFY_PROMPT = """You are an expert in system integration configuration.
You will receive the CURRENT connector config as JSON and an instruction. Output the
COMPLETE modified config object as JSON — repeat every key that should remain;
anything you omit is removed. Never include passwords, tokens, keys or any other
credential — credentials are managed separately and are not part of the config.
Output ONLY valid JSON, no markdown fences: {"config": {...}}"""


def validate_connector_config(config: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["Config is not an object"]
    if len(json.dumps(config, default=str)) > MAX_CONFIG_BYTES:
        errors.append("Modified config is too large to review")

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                low = str(k).lower()
                for part in FORBIDDEN_CONFIG_KEY_PARTS:
                    if part in low:
                        errors.append(f"Config key {path + str(k)!r} looks like a "
                                      f"credential ({part!r}) — credentials are "
                                      f"never draftable")
                        break
                walk(v, f"{path}{k}.")
        elif isinstance(obj, list):
            for item in obj:
                walk(item, path)

    walk(config, "")
    return errors


async def generate_connector_modification(
    description: str, *, current_name: str, connector_type: str,
    current_config: dict[str, Any],
) -> dict[str, Any]:
    """The prompt receives ONLY name/type/config — credentials never leave the
    encrypted column, in either direction."""
    description = _check_prompt(description)
    current = {"name": current_name, "connector_type": connector_type,
               "config": current_config or {}}
    raw = await _ai_generate_json(
        prompt=_current_block(current, description), system=CONNECTOR_MODIFY_PROMPT)
    if not raw or not isinstance(raw, dict):
        raise ModifyDraftError(_AI_DOWN)

    config = raw.get("config") if isinstance(raw.get("config"), dict) else raw
    return {"draft": {"config": config},
            "errors": validate_connector_config(config)}
