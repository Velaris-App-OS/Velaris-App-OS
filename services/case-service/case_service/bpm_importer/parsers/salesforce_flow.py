"""Salesforce Flow parser — FlowDefinition XML (Force.com Metadata API).

Extracts: Flow elements (screens, decisions, assignments, record operations),
Screen → form with fields, Decision → routing + rule, RecordCreate/Update → automated step.

Security: defusedxml (SEC-1), file size check.
"""
from __future__ import annotations

import logging

import defusedxml.ElementTree as ET

from case_service.hxmigrate.security import MAX_XML_BYTES

logger = logging.getLogger(__name__)

_SF_NS = "http://soap.sforce.com/2006/04/metadata"


def parse_files(files: list[dict]) -> dict:
    result: dict[str, list] = {}
    for f in files:
        if f.get("rule_type") not in ("SalesforceFlow", "Other"):
            continue
        content = f.get("content", "")
        if not content.strip():
            continue
        if len(content.encode("utf-8", errors="replace")) > MAX_XML_BYTES:
            logger.warning("Salesforce Flow file too large, skipping")
            continue
        try:
            parsed = _parse_flow(f["name"], content)
            if parsed:
                result.setdefault("SalesforceFlow", []).append(parsed)
        except Exception as e:
            logger.warning("Salesforce parse failed for %s: %s", f["name"], type(e).__name__)
    return result


def _safe_parse(content: str):
    try:
        return ET.fromstring(content)
    except Exception as e:
        logger.warning("Salesforce XML parse error: %s", type(e).__name__)
        return None


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _child(el, name: str, default: str = "") -> str:
    # Use 'is not None' — in Python 3.12, elements with only text content are falsy
    child = el.find(f"{{{_SF_NS}}}{name}")
    if child is None:
        child = el.find(name)
    return (getattr(child, "text", None) or default).strip()[:2000]


def _parse_flow(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None

    flow_name  = _child(root, "label") or _child(root, "processType") or name.split("/")[-1].replace("-meta.xml", "")
    flow_type  = _child(root, "processType") or "Flow"
    start_ref  = _child(root, "startElementReference")

    steps:  list[dict] = []
    forms:  list[dict] = []
    rules:  list[dict] = []

    # Index all named elements
    element_map: dict[str, object] = {}
    for el in root:
        ename = _child(el, "name")
        if ename:
            element_map[ename] = el

    # Parse each element type
    order = 0
    for el in root:
        local = _local(el.tag)
        ename = _child(el, "name")
        label = _child(el, "label") or ename

        if local == "screens":
            form   = _parse_screen(ename, label, el)
            step   = {
                "id":        ename,
                "name":      label,
                "step_type": "user_task",
                "order":     order,
                "form_key":  ename,
            }
            if form:
                forms.append(form)
            steps.append(step)
            order += 1

        elif local == "decisions":
            rule   = _parse_decision(ename, label, el)
            if rule:
                rules.append(rule)
            steps.append({
                "id":        ename,
                "name":      label,
                "step_type": "routing",
                "order":     order,
                "form_key":  None,
            })
            order += 1

        elif local in ("recordCreates", "recordUpdates", "recordDeletes", "recordLookups"):
            action_label = {
                "recordCreates": f"Create: {label}",
                "recordUpdates": f"Update: {label}",
                "recordDeletes": f"Delete: {label}",
                "recordLookups": f"Query: {label}",
            }.get(local, label)
            steps.append({
                "id":        ename,
                "name":      action_label,
                "step_type": "automated",
                "order":     order,
                "form_key":  None,
            })
            order += 1

        elif local == "subflows":
            steps.append({
                "id":        ename,
                "name":      label,
                "step_type": "subprocess",
                "order":     order,
                "form_key":  None,
            })
            order += 1

        elif local == "actionCalls":
            atype = _child(el, "actionType") or "apex"
            steps.append({
                "id":        ename,
                "name":      label,
                "step_type": "automated",
                "order":     order,
                "form_key":  None,
                "connector_hint": atype[:100],
            })
            order += 1

    # Order using connector references (next element chains)
    steps = _order_by_connectors(steps, root)

    stages = [{"id": "main", "name": flow_name, "steps": steps}]
    return {
        "name":      flow_name,
        "rule_type": "SalesforceFlow",
        "vendor":    "salesforce",
        "flow_type": flow_type,
        "stages":    stages,
        "forms":     forms,
        "rules":     rules,
    }


def _parse_screen(eid: str, label: str, el) -> dict | None:
    fields: list[dict] = []
    for field_el in el.iter():
        if _local(field_el.tag) != "fields":
            continue
        fkey  = _child(field_el, "name")
        flabel = _child(field_el, "fieldText") or _child(field_el, "label") or fkey
        ftype  = _child(field_el, "fieldType") or "InputField"
        required = _child(field_el, "isRequired") == "true"
        if fkey:
            fields.append({
                "field_key":  fkey[:200],
                "label":      flabel[:500],
                "field_type": _sf_field_type(ftype),
                "required":   required,
            })
    if not fields:
        return None
    return {
        "form_key": eid,
        "name":     label,
        "sections": [{"title": label, "fields": fields[:200]}],
    }


def _parse_decision(eid: str, label: str, el) -> dict | None:
    rules: list[dict] = []
    for rule_el in el.iter():
        if _local(rule_el.tag) != "rules":
            continue
        cond_text = _child(rule_el, "label") or ""
        for cond_el in rule_el.iter():
            if _local(cond_el.tag) == "conditions":
                left  = _child(cond_el, "leftValueReference")
                op    = _child(cond_el, "operator")
                right = _child(cond_el, "rightValue")
                if left or op:
                    cond_text += f" {left} {op} {right}".strip()
        rules.append({"condition": cond_text[:500], "result": _child(rule_el, "connector")})
    if not rules:
        return None
    return {
        "name":       label,
        "rule_type":  "SalesforceDecision",
        "conditions": rules[:50],
    }


def _sf_field_type(ftype: str) -> str:
    mapping = {
        "InputField":     "text",
        "TextBox":        "text",
        "LargeTextArea":  "textarea",
        "Number":         "number",
        "Currency":       "number",
        "Date":           "date",
        "DateTime":       "date",
        "Checkbox":       "checkbox",
        "DropdownBox":    "select",
        "RadioButtons":   "select",
        "MultiSelectCheckboxes": "select",
        "PasswordField":  "text",
    }
    return mapping.get(ftype, "text")


def _order_by_connectors(steps: list[dict], root) -> list[dict]:
    """Reorder steps by following 'connector' references (next element chains)."""
    try:
        next_map: dict[str, str] = {}
        for el in root:
            eid = None
            for child in el:
                if _local(child.tag) == "name":
                    eid = (child.text or "").strip()
                elif _local(child.tag) == "connector":
                    for sub in child:
                        if _local(sub.tag) == "targetReference":
                            if eid:
                                next_map[eid] = (sub.text or "").strip()

        if not next_map:
            return steps

        id_to_step  = {s["id"]: s for s in steps}
        all_targets = set(next_map.values())
        starts      = [s["id"] for s in steps if s["id"] not in all_targets]

        ordered: list[dict] = []
        seen: set[str] = set()
        queue = list(starts) or ([steps[0]["id"]] if steps else [])
        while queue:
            sid = queue.pop(0)
            if sid in seen or sid not in id_to_step:
                continue
            seen.add(sid)
            ordered.append(id_to_step[sid])
            nxt = next_map.get(sid)
            if nxt:
                queue.append(nxt)
        for s in steps:
            if s["id"] not in seen:
                ordered.append(s)

        for i, s in enumerate(ordered):
            s["order"] = i
        return ordered

    except Exception:
        return steps
