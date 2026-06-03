"""Pass 4: Generate.

Converts mapped rules into Helix-native object definitions.
Raw SQL generation has been removed (SEC-9) — all creation now goes through
the Creator module (hxmigrate/creator.py) which uses Velaris REST APIs.

Output is JSON-only, safe to store in JSONB columns.
"""
from __future__ import annotations

import re
import uuid


def generate(mapped: dict) -> dict:
    """Pass 4: produce Helix-native object definitions from mapped rules."""
    case_types = [_gen_case_type(ct) for ct in mapped.get("case_types", [])]
    forms      = [_gen_form(f)       for f  in mapped.get("forms", [])]
    sla_rules  = [_gen_sla(s)        for s  in mapped.get("sla_rules", [])]
    ag_defs    = [_gen_ag(a)         for a  in mapped.get("access_groups", [])]

    return {
        "case_types":    case_types,
        "forms":         forms,
        "sla_rules":     sla_rules,
        "access_groups": ag_defs,
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "case"


def _gen_case_type(ct: dict) -> dict:
    stages = []
    for s in ct.get("stages", []):
        steps = []
        for st in s.get("steps", []):
            step: dict = {
                "id":        _slug(st["name"]),
                "name":      st["name"],
                "step_type": st.get("step_type", "user_task"),
                "required":  True,
            }
            if st.get("form_key"):
                step["form_key"] = st["form_key"]
            if st.get("conditions"):
                step["conditions"] = st["conditions"][:20]
            steps.append(step)
        stages.append({
            "id":    s.get("id") or _slug(s["name"]),
            "name":  s["name"],
            "steps": steps,
        })

    if not stages:
        stages = [{"id": "main", "name": "Main", "steps": []}]

    name = ct["name"]
    return {
        "name":            name,
        "slug":            _slug(name),
        "version":         "1.0.0",
        "description":     f"Imported from {ct.get('source_rule', 'BPM')}",
        "confidence":      ct.get("confidence", "partial"),
        "definition_json": {"stages": stages},
        "portal_enabled":  False,
        "tags":            ["imported"],
    }


def _gen_form(f: dict) -> dict:
    fields = []
    for field in f.get("fields", []):
        fkey = field.get("field_key") or _slug(field.get("name", "field"))
        fields.append({
            "id":       fkey,
            "label":    field.get("label") or field.get("name", "Field"),
            "type":     field.get("field_type") or field.get("type", "text"),
            "required": field.get("required", False),
        })
    name = f["name"]
    return {
        "name":       name,
        "slug":       _slug(name),
        "confidence": f.get("confidence", "partial"),
        "schema":     {"title": name, "fields": fields},
    }


def _gen_sla(s: dict) -> dict:
    return {
        "name":             s["name"],
        "goal_seconds":     int(s.get("goal_hours", 24) * 3600),
        "deadline_seconds": int(s.get("deadline_hours", 48) * 3600),
        "escalation_to":    s.get("escalation_to", ""),
        "active":           True,
    }


def _gen_ag(a: dict) -> dict:
    return {
        "name":       a["name"],
        "roles":      a.get("roles", [])[:50],
        "is_default": False,
    }
