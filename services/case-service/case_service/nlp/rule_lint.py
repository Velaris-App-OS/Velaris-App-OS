"""HxDraft P3 — deterministic lint of a VALID rule draft against its case type.

Advisory only (signed off 2026-07-05): lint findings render as their own list on
the card and never block Apply — variable path conventions vary enough that a
false lint must not stop a reviewed apply. No LLM involvement; every finding is a
plain structural check:

  * a condition ``field_path`` that resolves to nothing on the case type
    (variables, step form fields);
  * a numeric operator on a variable whose declared type can't order
    ("this rule can never fire — claim.amount is text on this case-type");
  * an ``advance_stage`` / ``skip_stage`` action whose target isn't a stage id.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

#: operators that require an orderable value
_NUMERIC_OPS = {"gt", "gte", "lt", "lte", "between"}
#: declared field types that can never satisfy an ordering comparison
_UNORDERABLE_TYPES = {"text", "textarea", "select", "multiselect", "boolean",
                      "email", "phone", "file"}
_STAGE_ACTIONS = {"advance_stage", "skip_stage"}


def _known_fields(definition: dict[str, Any]) -> dict[str, str]:
    """id → declared field_type, from case-type variables and step form fields."""
    fields: dict[str, str] = {}
    for var in definition.get("variables", []) or []:
        if isinstance(var, dict) and var.get("id"):
            fields[str(var["id"])] = str(var.get("field_type") or "")
    for stage in definition.get("stages", []) or []:
        if not isinstance(stage, dict):
            continue
        for step in stage.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            for f in step.get("form_fields", []) or []:
                if isinstance(f, dict) and f.get("id"):
                    fields.setdefault(str(f["id"]), str(f.get("field_type") or ""))
    return fields


def _resolve(field_path: str, fields: dict[str, str]) -> str | None:
    """Match a dotted path to a declared field: full path, case.data.-stripped
    path, or the last segment. Returns the declared type, or None if unknown."""
    path = field_path.strip()
    low = path.lower()
    if low.startswith("case.data."):
        path = path[len("case.data."):]
    for candidate in (path, path.rsplit(".", 1)[-1]):
        if candidate in fields:
            return fields[candidate]
    return None


def lint_rule_draft(draft: dict[str, Any],
                    case_type_definition: dict[str, Any]) -> list[str]:
    """Advisory findings for a rule draft against its scoped case type."""
    definition = case_type_definition or {}
    d = (draft or {}).get("definition_json") or {}
    findings: list[str] = []

    fields = _known_fields(definition)
    for c in d.get("conditions", []) or []:
        if not isinstance(c, dict):
            continue
        path = str(c.get("field_path") or "").strip()
        if not path:
            continue
        declared = _resolve(path, fields)
        if declared is None:
            findings.append(f"'{path}' is not a declared variable or form field on "
                            f"this case-type — the condition may never match")
        elif c.get("operator") in _NUMERIC_OPS and declared in _UNORDERABLE_TYPES:
            findings.append(f"this rule can never fire — '{path}' is {declared} on "
                            f"this case-type and '{c.get('operator')}' needs an "
                            f"orderable value")

    stage_ids = {s.get("id") for s in definition.get("stages", []) or []
                 if isinstance(s, dict)}
    for a in d.get("actions", []) or []:
        if isinstance(a, dict) and a.get("action_type") in _STAGE_ACTIONS:
            target = str(a.get("target") or "").strip()
            if target and target not in stage_ids:
                findings.append(f"action '{a['action_type']}' targets '{target}', "
                                f"which is not a stage of this case-type")
    return findings
