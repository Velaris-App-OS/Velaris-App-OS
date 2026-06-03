"""Appian deep parser — full XML/JSON extraction.

Extracts: ProcessModel (process), Interface (form), RecordType (data model),
ExpressionRule (business rule), Decision (decision table),
Integration (external connector hint), Group (access group).

Security: defusedxml (SEC-1), JSON depth/size limits (SEC-3), file size check.
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
        rule_type = f.get("rule_type", "Other")
        content   = f.get("content", "")
        if not content.strip():
            continue
        try:
            parsed = _dispatch(rule_type, f["name"], content)
            if parsed:
                result.setdefault(rule_type, []).append(parsed)
        except Exception as e:
            logger.warning("Appian parse failed for %s: %s", f["name"], type(e).__name__)
    return result


def _dispatch(rule_type: str, name: str, content: str) -> dict | None:
    if rule_type == "ProcessModel":
        return _parse_process_model(name, content)
    if rule_type == "Interface":
        return _parse_interface(name, content)
    if rule_type == "RecordType":
        return _parse_record_type(name, content)
    if rule_type == "ExpressionRule":
        return _parse_expression_rule(name, content)
    if rule_type == "Decision":
        return _parse_decision(name, content)
    if rule_type == "IntegrationObject":
        return _parse_integration(name, content)
    if rule_type == "Group":
        return _parse_group(name, content)
    return None


def _safe_xml(content: str):
    if len(content.encode("utf-8", errors="replace")) > MAX_XML_BYTES:
        logger.warning("Appian XML too large, skipping")
        return None
    try:
        return ET.fromstring(content)
    except Exception as e:
        logger.warning("Appian XML parse error: %s", type(e).__name__)
        return None


def _safe_json(content: str) -> dict | None:
    if len(content.encode("utf-8", errors="replace")) > MAX_JSON_BYTES:
        logger.warning("Appian JSON too large, skipping")
        return None
    try:
        data = json.loads(content)
        check_json_depth(data)  # SEC-3
        return data if isinstance(data, dict) else None
    except (ValueError, json.JSONDecodeError) as e:
        return None


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _attr_or_child(el, *names: str, default: str = "") -> str:
    for n in names:
        val = el.get(n)
        if val:
            return val[:2000]
        child = el.find(f".//{n}") or el.find(n)
        if child is not None and child.text:
            return child.text.strip()[:2000]
    return default


# ── ProcessModel → process ────────────────────────────────────────────────────

def _parse_process_model(name: str, content: str) -> dict | None:
    root = _safe_xml(content)
    if root is None:
        # Try JSON format
        data = _safe_json(content)
        if data:
            return _parse_process_model_json(name, data)
        return None
    return _parse_process_model_xml(name, root)


def _parse_process_model_xml(name: str, root) -> dict | None:
    pm_name = root.get("name") or root.get("uuid") or name.split("/")[-1]
    stages: list[dict] = []
    steps:  list[dict] = []

    for el in root.iter():
        local = _local(el.tag)

        if local in ("stage", "Stage", "swimlane", "Swimlane"):
            stages.append({
                "id":   el.get("id") or el.get("uuid") or f"stage_{len(stages)}",
                "name": el.get("name") or el.get("label") or f"Stage {len(stages)+1}",
            })

        elif local in ("node", "Node", "activity", "Activity", "task", "Task"):
            ntype     = (el.get("type") or el.get("nodeType") or "user").lower()
            node_name = el.get("name") or el.get("label") or el.get("id") or local
            steps.append({
                "id":          el.get("id") or el.get("uuid") or "",
                "name":        node_name,
                "step_type":   _appian_step_type(ntype),
                "form_key":    _extract_appian_form_ref(el),
                "stage_id":    el.get("swimlane") or el.get("stageId") or "",
                "order":       int(el.get("order") or el.get("sequence") or 0),
            })

    if not stages and steps:
        stages = [{"id": "main", "name": pm_name}]

    stage_steps: dict[str, list] = {s["id"]: [] for s in stages}
    unassigned: list[dict] = []
    for step in sorted(steps, key=lambda s: s.get("order", 0)):
        sid = step.get("stage_id", "")
        if sid in stage_steps:
            stage_steps[sid].append(step)
        else:
            unassigned.append(step)

    for stage in stages:
        stage["steps"] = stage_steps.get(stage["id"], [])
    if stages and unassigned:
        stages[-1]["steps"].extend(unassigned)

    return {"name": pm_name, "rule_type": "ProcessModel", "stages": stages}


def _parse_process_model_json(name: str, data: dict) -> dict | None:
    pm_name = data.get("name") or data.get("uuid") or name
    stages  = []
    for s in data.get("stages", data.get("swimlanes", [])):
        if isinstance(s, dict):
            stages.append({
                "id":    s.get("id") or s.get("uuid") or f"stage_{len(stages)}",
                "name":  s.get("name") or s.get("label") or f"Stage {len(stages)+1}",
                "steps": [],
            })
    return {"name": pm_name, "rule_type": "ProcessModel", "stages": stages}


def _appian_step_type(ntype: str) -> str:
    if "user" in ntype or "form" in ntype:
        return "user_task"
    if "service" in ntype or "script" in ntype or "integration" in ntype:
        return "automated"
    if "approval" in ntype or "vote" in ntype:
        return "approval"
    if "sub" in ntype:
        return "subprocess"
    return "user_task"


def _extract_appian_form_ref(el) -> str | None:
    form = el.get("formId") or el.get("interface") or el.get("formRef")
    return form[:200] if form else None


# ── Interface → form ──────────────────────────────────────────────────────────

def _parse_interface(name: str, content: str) -> dict | None:
    root = _safe_xml(content)
    if root is None:
        data = _safe_json(content)
        if data:
            return _parse_interface_json(name, data)
        return None

    iface_name = root.get("name") or name.split("/")[-1]
    fields: list[dict] = []
    for el in root.iter():
        local = _local(el.tag)
        if local in ("field", "Field", "component", "Component"):
            fkey = el.get("name") or el.get("id") or el.get("saveInto") or ""
            label = el.get("label") or el.get("name") or fkey
            if fkey:
                fields.append({
                    "field_key":  fkey[:200],
                    "label":      label[:500],
                    "field_type": _appian_field_type(el.get("type") or "TextField"),
                    "required":   (el.get("required") or "").lower() == "true",
                })
    return {"name": iface_name, "rule_type": "Interface", "fields": fields[:200]}


def _parse_interface_json(name: str, data: dict) -> dict | None:
    iface_name = data.get("name") or name
    fields: list[dict] = []
    for comp in data.get("components", data.get("fields", [])):
        if not isinstance(comp, dict):
            continue
        fkey = comp.get("name") or comp.get("saveInto") or comp.get("id") or ""
        if fkey:
            fields.append({
                "field_key":  fkey[:200],
                "label":      (comp.get("label") or fkey)[:500],
                "field_type": _appian_field_type(comp.get("type") or "TextField"),
                "required":   comp.get("required", False),
            })
    return {"name": iface_name, "rule_type": "Interface", "fields": fields[:200]}


def _appian_field_type(appian_type: str) -> str:
    t = appian_type.lower()
    if "dropdown" in t or "select" in t or "radio" in t:
        return "select"
    if "date" in t:
        return "date"
    if "integer" in t or "decimal" in t or "float" in t:
        return "number"
    if "checkbox" in t or "boolean" in t:
        return "checkbox"
    if "paragraph" in t or "richtext" in t or "textarea" in t:
        return "textarea"
    return "text"


# ── RecordType → data model ───────────────────────────────────────────────────

def _parse_record_type(name: str, content: str) -> dict | None:
    root = _safe_xml(content)
    if root is None:
        data = _safe_json(content)
        if not data:
            return None
        rt_name = data.get("name") or name
        fields = [
            {"field_key": f.get("name", "")[:200], "label": (f.get("label") or f.get("name", ""))[:500],
             "data_type": (f.get("type") or "string")[:50]}
            for f in data.get("fields", []) if isinstance(f, dict) and f.get("name")
        ]
        return {"name": rt_name, "rule_type": "RecordType", "fields": fields[:200]}

    rt_name = root.get("name") or name.split("/")[-1]
    fields: list[dict] = []
    for el in root.iter():
        if _local(el.tag) in ("field", "Field", "attribute", "Attribute"):
            fname = el.get("name") or el.get("id") or ""
            ftype = (el.get("type") or el.get("dataType") or "string").lower()
            if fname:
                fields.append({"field_key": fname[:200], "label": fname[:500], "data_type": ftype[:50]})
    return {"name": rt_name, "rule_type": "RecordType", "fields": fields[:200]}


# ── ExpressionRule → business rule ───────────────────────────────────────────

def _parse_expression_rule(name: str, content: str) -> dict | None:
    root = _safe_xml(content)
    rule_name = name.split("/")[-1]
    expression = ""
    if root is not None:
        rule_name  = root.get("name") or rule_name
        expression = (root.get("body") or root.get("expression") or "")[:5000]
        if not expression:
            for el in root.iter():
                if _local(el.tag) in ("body", "expression", "script"):
                    expression = (el.text or "")[:5000]
                    break
    return {
        "name":      rule_name,
        "rule_type": "ExpressionRule",
        "expression": expression,
        "rule_category": "expression",
    }


# ── Decision → decision table ─────────────────────────────────────────────────

def _parse_decision(name: str, content: str) -> dict | None:
    root = _safe_xml(content)
    dec_name   = name.split("/")[-1]
    conditions: list[dict] = []
    if root is not None:
        dec_name = root.get("name") or dec_name
        for el in root.iter():
            if _local(el.tag) in ("row", "Row", "rule", "Rule"):
                cond = (el.get("condition") or el.get("when") or "")[:500]
                result = (el.get("result") or el.get("then") or "")[:500]
                if cond or result:
                    conditions.append({"condition": cond, "result": result})
    return {
        "name":       dec_name,
        "rule_type":  "Decision",
        "conditions": conditions[:100],
    }


# ── Integration → connector hint ─────────────────────────────────────────────

def _parse_integration(name: str, content: str) -> dict | None:
    root = _safe_xml(content)
    int_name = name.split("/")[-1]
    endpoint = ""
    method   = "GET"
    if root is not None:
        int_name = root.get("name") or int_name
        endpoint = (root.get("url") or root.get("endpoint") or "")[:500]
        method   = (root.get("method") or "GET").upper()[:10]
    return {
        "name":       int_name,
        "rule_type":  "IntegrationObject",
        "endpoint":   endpoint,
        "method":     method,
        "connector_hint": "http_custom",
    }


# ── Group → access group ──────────────────────────────────────────────────────

def _parse_group(name: str, content: str) -> dict | None:
    root = _safe_xml(content)
    if root is None:
        return None
    g_name = root.get("name") or name.split("/")[-1]
    members: list[str] = []
    for el in root.iter():
        if _local(el.tag) in ("member", "Member", "user", "User"):
            m = el.get("name") or el.get("username") or (el.text or "").strip()
            if m:
                members.append(m[:200])
    return {
        "name":      g_name,
        "rule_type": "Group",
        "roles":     members[:50],
    }
