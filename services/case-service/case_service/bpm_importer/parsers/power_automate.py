"""Power Automate parser — Flow Definition v1 JSON.

Parses Microsoft Power Automate cloud flow JSON exports.
Extracts triggers, actions, conditions, and approval steps.

Security: JSON depth/size limits (SEC-3), no arbitrary code execution.
"""
from __future__ import annotations

import json
import logging

from case_service.hxmigrate.security import MAX_JSON_BYTES, check_json_depth

logger = logging.getLogger(__name__)


def parse_files(files: list[dict]) -> dict:
    result: dict[str, list] = {}
    for f in files:
        if f.get("rule_type") not in ("FlowDefinition", "Other"):
            continue
        content = f.get("content", "")
        if not content.strip():
            continue
        try:
            parsed = _parse_flow(f["name"], content)
            if parsed:
                result.setdefault("FlowDefinition", []).append(parsed)
        except Exception as e:
            logger.warning("Power Automate parse failed for %s: %s", f["name"], type(e).__name__)
    return result


def _safe_json(content: str) -> dict | None:
    if len(content.encode("utf-8", errors="replace")) > MAX_JSON_BYTES:
        logger.warning("Power Automate JSON too large, skipping")
        return None
    try:
        data = json.loads(content)
        check_json_depth(data)  # SEC-3
        return data if isinstance(data, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


def _parse_flow(name: str, content: str) -> dict | None:
    data = _safe_json(content)
    if not data:
        return None

    # Power Automate flow can be wrapped in a properties envelope
    flow_def = (
        data.get("definition")
        or data.get("properties", {}).get("definition")
        or data
    )

    flow_name = (
        data.get("name")
        or data.get("displayName")
        or data.get("properties", {}).get("displayName")
        or name.split("/")[-1].replace(".json", "")
    )

    triggers = flow_def.get("triggers", {})
    actions  = flow_def.get("actions", {})

    steps:  list[dict] = []
    forms:  list[dict] = []
    rules:  list[dict] = []

    # ── Trigger → first step ──────────────────────────────────────────────────
    for trigger_name, trigger in (triggers.items() if isinstance(triggers, dict) else []):
        ttype = (trigger.get("type") or "").lower()
        steps.append({
            "id":        f"trigger_{trigger_name}",
            "name":      _clean_name(trigger_name),
            "step_type": _pa_trigger_type(ttype),
            "order":     0,
            "form_key":  None,
        })

    # ── Actions → steps ───────────────────────────────────────────────────────
    _flatten_actions(actions, steps, forms, rules, order_start=1)

    # Build single-stage structure
    sorted_steps = sorted(steps, key=lambda s: s.get("order", 0))
    stages = [{"id": "main", "name": flow_name, "steps": sorted_steps}]

    return {
        "name":      flow_name,
        "rule_type": "FlowDefinition",
        "vendor":    "power_automate",
        "stages":    stages,
        "forms":     forms,
        "rules":     rules,
    }


def _flatten_actions(
    actions: dict | list,
    steps: list,
    forms: list,
    rules: list,
    order_start: int = 1,
    depth: int = 0,
) -> int:
    """Recursively flatten nested Power Automate actions into a flat step list."""
    if depth > 10 or not actions:
        return order_start

    items = actions.items() if isinstance(actions, dict) else enumerate(actions)
    order = order_start

    for key, action in items:
        if not isinstance(action, dict):
            continue
        atype = (action.get("type") or "").lower()
        aname = _clean_name(str(key))

        step: dict = {
            "id":        str(key),
            "name":      aname,
            "step_type": _pa_action_type(atype),
            "order":     order,
            "form_key":  None,
        }

        # Approval action → approval step + form extraction
        if "approval" in atype:
            step["step_type"] = "approval"
            form = _extract_approval_form(aname, action)
            if form:
                forms.append(form)
                step["form_key"] = form.get("form_key")

        # Condition → routing step + rule
        elif "condition" in atype or atype == "if":
            step["step_type"] = "routing"
            expr = action.get("expression") or str(action.get("inputs", {}).get("expression", ""))
            if expr:
                rules.append({
                    "name":       f"Condition: {aname}",
                    "rule_type":  "Condition",
                    "expression": str(expr)[:2000],
                })
            # Recurse into true/false branches
            inputs = action.get("actions", {})
            else_branch = action.get("else", {}).get("actions", {})
            order = _flatten_actions(inputs,      steps, forms, rules, order + 1, depth + 1)
            order = _flatten_actions(else_branch, steps, forms, rules, order,     depth + 1)
            continue

        # Switch/foreach → flatten children
        elif atype in ("switch", "foreach", "scope", "until"):
            inner = action.get("actions", {}) or action.get("cases", {})
            order = _flatten_actions(inner, steps, forms, rules, order + 1, depth + 1)
            continue

        steps.append(step)
        order += 1

    return order


def _pa_trigger_type(ttype: str) -> str:
    if "manual" in ttype or "button" in ttype or "form" in ttype:
        return "user_task"
    if "schedule" in ttype or "recurrence" in ttype:
        return "automated"
    return "user_task"


def _pa_action_type(atype: str) -> str:
    if "approval" in atype:
        return "approval"
    if "http" in atype or "rest" in atype or "connector" in atype:
        return "automated"
    if "send" in atype or "mail" in atype or "notification" in atype:
        return "automated"
    if "initialize" in atype or "set" in atype or "parse" in atype:
        return "automated"
    if "compose" in atype or "select" in atype or "filter" in atype:
        return "automated"
    return "automated"


def _extract_approval_form(name: str, action: dict) -> dict | None:
    inputs = action.get("inputs", {})
    if not isinstance(inputs, dict):
        return None
    title   = inputs.get("title") or inputs.get("subject") or name
    details = inputs.get("details") or inputs.get("body") or ""
    # Build a minimal form with title + details fields
    return {
        "form_key": f"approval_{name.lower().replace(' ', '_')[:50]}",
        "name":     f"Approval: {title[:200]}",
        "sections": [
            {
                "title": "Approval Request",
                "fields": [
                    {"field_key": "title",   "label": "Title",   "field_type": "text",     "required": True},
                    {"field_key": "details", "label": "Details", "field_type": "textarea",  "required": False},
                ],
            }
        ],
    }


def _clean_name(key: str) -> str:
    """Convert Power Automate action key to human-readable name."""
    name = key.replace("_", " ").replace("-", " ")
    # CamelCase splitting
    import re
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return name.strip().title()[:200]
