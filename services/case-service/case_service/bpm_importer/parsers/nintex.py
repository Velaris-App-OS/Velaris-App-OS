"""Nintex parser — XML/NWC workflow format.

Handles both Nintex for SharePoint (XML) and Nintex Workflow Cloud (NWC JSON).
Extracts: workflow actions, forms, conditions, task forms.

Security: defusedxml (SEC-1), JSON depth/size limits (SEC-3), file size checks.
"""
from __future__ import annotations

import json
import logging

import defusedxml.ElementTree as ET

from case_service.hxmigrate.security import MAX_XML_BYTES, MAX_JSON_BYTES, check_json_depth

logger = logging.getLogger(__name__)


def parse_files(files: list[dict]) -> dict:
    result: dict[str, list] = {}
    for f in files:
        if f.get("rule_type") not in ("NintexWorkflow", "Other"):
            continue
        content = f.get("content", "")
        if not content.strip():
            continue
        try:
            parsed = _parse_nintex(f["name"], content)
            if parsed:
                result.setdefault("NintexWorkflow", []).append(parsed)
        except Exception as e:
            logger.warning("Nintex parse failed for %s: %s", f["name"], type(e).__name__)
    return result


def _parse_nintex(name: str, content: str) -> dict | None:
    # Try JSON first (NWC format)
    if content.strip().startswith("{"):
        return _parse_nwc_json(name, content)
    # Fall back to XML (SharePoint format)
    return _parse_xml(name, content)


# ── NWC JSON ──────────────────────────────────────────────────────────────────

def _parse_nwc_json(name: str, content: str) -> dict | None:
    if len(content.encode("utf-8", errors="replace")) > MAX_JSON_BYTES:
        logger.warning("Nintex NWC JSON too large, skipping")
        return None
    try:
        data = json.loads(content)
        check_json_depth(data)  # SEC-3
    except (ValueError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    wf_name  = data.get("name") or data.get("workflowName") or name
    steps:   list[dict] = []
    forms:   list[dict] = []

    actions = data.get("actions") or data.get("workflow", {}).get("actions") or []
    if isinstance(actions, list):
        for i, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            steps.append(_nwc_action_to_step(action, i))
    elif isinstance(actions, dict):
        for i, (key, action) in enumerate(actions.items()):
            if not isinstance(action, dict):
                continue
            a = dict(action)
            a.setdefault("name", key)
            steps.append(_nwc_action_to_step(a, i))

    stages = [{"id": "main", "name": wf_name, "steps": steps}]
    return {
        "name":      wf_name,
        "rule_type": "NintexWorkflow",
        "vendor":    "nintex",
        "source":    "NWC",
        "stages":    stages,
        "forms":     forms,
    }


def _nwc_action_to_step(action: dict, order: int) -> dict:
    atype = (action.get("actionType") or action.get("type") or "").lower()
    label = action.get("name") or action.get("label") or f"Step {order+1}"
    return {
        "id":        action.get("id") or f"step_{order}",
        "name":      label[:200],
        "step_type": _nwc_step_type(atype),
        "order":     order,
        "form_key":  action.get("formId") or action.get("taskFormId"),
    }


def _nwc_step_type(atype: str) -> str:
    # Check approval BEFORE task — "approvalTask" must map to approval, not user_task
    if "approval" in atype or "review" in atype:
        return "approval"
    if "assign" in atype or "task" in atype or "form" in atype:
        return "user_task"
    if "email" in atype or "notification" in atype or "send" in atype:
        return "automated"
    if "condition" in atype or "decision" in atype:
        return "routing"
    if "call" in atype or "web" in atype or "service" in atype:
        return "automated"
    return "automated"


# ── SharePoint XML ────────────────────────────────────────────────────────────

def _parse_xml(name: str, content: str) -> dict | None:
    if len(content.encode("utf-8", errors="replace")) > MAX_XML_BYTES:
        logger.warning("Nintex XML too large, skipping")
        return None
    try:
        root = ET.fromstring(content)
    except Exception as e:
        logger.warning("Nintex XML parse error: %s", type(e).__name__)
        return None

    wf_name = root.get("Name") or root.get("name") or name.split("/")[-1]
    steps:  list[dict] = []
    forms:  list[dict] = []

    for el in root.iter():
        local = _local(el.tag)
        if local in ("Activity", "action", "Action", "WFActivity"):
            aid   = el.get("id") or el.get("Id") or f"act_{len(steps)}"
            label = el.get("Name") or el.get("name") or el.get("Id") or local
            atype = (el.get("ActionId") or el.get("type") or "").lower()
            steps.append({
                "id":        aid,
                "name":      label[:200],
                "step_type": _sp_step_type(atype),
                "order":     int(el.get("Sequence") or el.get("sequence") or len(steps)),
                "form_key":  el.get("TaskFormUrl") or el.get("FormUrl") or None,
            })

        elif local in ("TaskForm", "Form", "StartForm"):
            form_name = el.get("Name") or el.get("Title") or local
            fields: list[dict] = []
            for field_el in el.iter():
                if _local(field_el.tag) in ("Field", "Column"):
                    fkey   = field_el.get("Name") or field_el.get("InternalName") or ""
                    flabel = field_el.get("DisplayName") or fkey
                    if fkey:
                        fields.append({
                            "field_key":  fkey[:200],
                            "label":      flabel[:500],
                            "field_type": "text",
                            "required":   False,
                        })
            if fields:
                form_key = el.get("id") or el.get("Name") or f"form_{len(forms)}"
                forms.append({
                    "form_key": form_key,
                    "name":     form_name[:200],
                    "sections": [{"title": form_name, "fields": fields[:100]}],
                })

    stages = [{"id": "main", "name": wf_name, "steps": sorted(steps, key=lambda s: s["order"])}]
    return {
        "name":      wf_name,
        "rule_type": "NintexWorkflow",
        "vendor":    "nintex",
        "source":    "SharePoint",
        "stages":    stages,
        "forms":     forms,
    }


def _sp_step_type(atype: str) -> str:
    if "assign" in atype or "task" in atype or "collect" in atype:
        return "user_task"
    if "approval" in atype or "flexi" in atype:
        return "approval"
    if "email" in atype or "notification" in atype:
        return "automated"
    if "condition" in atype or "decision" in atype:
        return "routing"
    return "automated"


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag
