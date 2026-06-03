"""Pega deep parser — full ruleset extraction.

Extracts: Flow (process), Section/Harness (forms with fields), SLARule,
AccessGroup, DecisionTable (business rule), Correspondence (email template),
DataPage (data model), Assignment (user/queue step), FlowAction (form for step).

Security: defusedxml (SEC-1), file size check.
"""
from __future__ import annotations

import logging

import defusedxml.ElementTree as ET

from case_service.hxmigrate.security import MAX_XML_BYTES

logger = logging.getLogger(__name__)


def parse_files(files: list[dict]) -> dict:
    result: dict[str, list] = {}
    for f in files:
        rule_type = f.get("rule_type", "Other")
        content   = f.get("content", "")
        if not content.strip():
            continue
        # SEC-1: size check
        if len(content.encode("utf-8", errors="replace")) > MAX_XML_BYTES:
            logger.warning("Pega file %s too large, skipping", f.get("name", "?"))
            continue
        try:
            if rule_type == "Other":
                # Try to handle pega:ruleSet container (single-file export)
                container_results = _parse_ruleset_container(f["name"], content)
                for rt, items in container_results.items():
                    result.setdefault(rt, []).extend(items)
                continue
            parsed = _dispatch(rule_type, f["name"], content)
            if parsed:
                result.setdefault(rule_type, []).append(parsed)
        except Exception as e:
            logger.warning("Pega parse failed for %s: %s", f["name"], type(e).__name__)
    return result


def _parse_ruleset_container(name: str, content: str) -> dict:
    """Parse a pega:ruleSet container XML that holds multiple rule elements."""
    result: dict[str, list] = {}
    root = _safe_parse(content)
    if root is None:
        return result

    root_local = _local(root.tag).lower()
    if "ruleset" not in root_local and "rules" not in root_local:
        # Not a container — try dispatching as a single rule using tag inference
        tag = _local(root.tag)
        inferred = _infer_rule_type(tag, root)
        if inferred:
            parsed = _dispatch(inferred, name, content)
            if parsed:
                result.setdefault(inferred, []).append(parsed)
        return result

    # Container: iterate child rules
    for child in root:
        child_type = child.get("type") or child.get("pyClassName") or _local(child.tag)
        inferred = _infer_rule_type(child_type, child)
        if not inferred:
            continue
        child_name = child.get("name") or child.get("pyLabel") or name
        try:
            import xml.etree.ElementTree as stdlib_ET
            child_xml = stdlib_ET.tostring(child, encoding="unicode")
            parsed = _dispatch(inferred, child_name, child_xml)
            if parsed:
                result.setdefault(inferred, []).append(parsed)
        except Exception as e:
            logger.warning("Pega container child parse failed: %s", type(e).__name__)
    return result


def _infer_rule_type(tag_or_type: str, el) -> str | None:
    """Infer Pega rule type from an element tag or type attribute."""
    t = (tag_or_type or "").lower()
    if "flow" in t and "flowaction" not in t:
        return "Flow"
    if "section" in t or "harness" in t:
        return "Section"
    if "slarule" in t or ("sla" in t and "rule" in t):
        return "SLARule"
    if "accessgroup" in t:
        return "AccessGroup"
    if "decisiontable" in t or "decision" in t:
        return "DecisionTable"
    if "correspondence" in t:
        return "Correspondence"
    if "datapage" in t or "data_page" in t:
        return "DataPage"
    if "assignment" in t:
        return "Assignment"
    if "datatransform" in t or "data_transform" in t:
        return "DataTransform"
    return None


def _dispatch(rule_type: str, name: str, content: str) -> dict | None:
    if rule_type == "Flow":
        return _parse_flow(name, content)
    if rule_type in ("Section", "Harness"):
        return _parse_form(name, content, rule_type)
    if rule_type == "SLARule":
        return _parse_sla(name, content)
    if rule_type == "AccessGroup":
        return _parse_access_group(name, content)
    if rule_type == "DecisionTable":
        return _parse_decision_table(name, content)
    if rule_type == "Correspondence":
        return _parse_correspondence(name, content)
    if rule_type == "DataPage":
        return _parse_data_page(name, content)
    if rule_type == "Assignment":
        return _parse_assignment(name, content)
    if rule_type == "DataTransform":
        return _parse_data_transform(name, content)
    return None


def _safe_parse(content: str):
    try:
        return ET.fromstring(content)
    except Exception as e:
        logger.warning("XML parse error: %s", type(e).__name__)
        return None


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


# ── Flow → process (case type) ────────────────────────────────────────────────

def _parse_flow(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None

    flow_name = root.get("name") or root.get("pyLabel") or name.split("/")[-1]
    stages: list[dict] = []
    steps:  list[dict] = []
    sequence_map: dict[str, str] = {}

    for el in root.iter():
        local = _local(el.tag)

        if local in ("Stage", "pxStage"):
            stages.append({
                "id":   el.get("id") or el.get("pxStageID") or f"stage_{len(stages)}",
                "name": el.get("name") or el.get("pyLabel") or f"Stage {len(stages)+1}",
            })

        elif local in ("Step", "pxStep", "FlowShape"):
            step_id   = el.get("id") or el.get("pxStepID") or ""
            step_name = el.get("name") or el.get("pyLabel") or el.get("pyStepLabel") or step_id
            next_step = el.get("pyNextStep") or el.get("nextStepID") or ""
            if step_id and next_step:
                sequence_map[step_id] = next_step

            steps.append({
                "id":            step_id,
                "name":          step_name,
                "step_type":     _pega_step_type(el),
                "form_key":      (el.get("pyFlowAction") or el.get("pyFormAction") or "")[:200] or None,
                "stage_id":      el.get("pyStageID") or el.get("stageID") or "",
                "assignee_type": _pega_assignee_type(el),
                "order":         int(el.get("order") or el.get("pxOrdinal") or 0),
            })

    if sequence_map and steps:
        steps = _order_steps_by_chain(steps, sequence_map)

    stage_steps: dict[str, list] = {s["id"]: [] for s in stages}
    unassigned: list[dict] = []
    for step in steps:
        sid = step.get("stage_id", "")
        if sid in stage_steps:
            stage_steps[sid].append(step)
        else:
            unassigned.append(step)

    if not stages:
        stages = [{"id": "main", "name": flow_name}]
        stage_steps["main"] = steps

    for stage in stages:
        stage["steps"] = stage_steps.get(stage["id"], [])
    if stages and unassigned:
        stages[-1]["steps"].extend(unassigned)

    return {"name": flow_name, "rule_type": "Flow", "stages": stages, "steps": steps}


def _pega_step_type(el) -> str:
    shape = (el.get("pyShapeType") or el.get("type") or "").lower()
    if "approval" in shape:
        return "approval"
    if "utility" in shape or "decision" in shape:
        return "automated"
    if "subprocess" in shape or "subflow" in shape:
        return "subprocess"
    return "user_task"


def _pega_assignee_type(el) -> str:
    if (el.get("pyWorkBasket") or "").strip():
        return "queue"
    return "user"


def _order_steps_by_chain(steps: list[dict], sequence_map: dict[str, str]) -> list[dict]:
    id_to_step = {s["id"]: s for s in steps if s["id"]}
    all_targets = set(sequence_map.values())
    starts = [s["id"] for s in steps if s["id"] and s["id"] not in all_targets]
    if not starts:
        return steps
    ordered: list[dict] = []
    seen: set[str] = set()
    queue = list(starts)
    while queue:
        sid = queue.pop(0)
        if sid in seen or sid not in id_to_step:
            continue
        seen.add(sid)
        ordered.append(id_to_step[sid])
        nxt = sequence_map.get(sid)
        if nxt:
            queue.append(nxt)
    for s in steps:
        if s["id"] not in seen:
            ordered.append(s)
    return ordered


# ── Section / Harness → form ──────────────────────────────────────────────────

def _parse_form(name: str, content: str, rule_type: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None
    form_name = root.get("name") or root.get("pyLabel") or name.split("/")[-1]
    fields: list[dict] = []
    for el in root.iter():
        local = _local(el.tag)
        if local in ("Field", "pxField", "FieldGroup"):
            fkey  = el.get("pyReference") or el.get("name") or el.get("id") or ""
            label = el.get("pyLabel") or el.get("label") or fkey
            ftype = _pega_field_type(el)
            if fkey:
                fields.append({
                    "field_key":  fkey[:200],
                    "label":      label[:500],
                    "field_type": ftype,
                    "required":   (el.get("pyRequired") or "").lower() == "true",
                })
    return {"name": form_name, "rule_type": rule_type, "fields": fields[:200]}


def _pega_field_type(el) -> str:
    ftype = (el.get("pyFieldType") or el.get("type") or "text").lower()
    return {"integer": "number", "decimal": "number", "date": "date",
            "datetime": "date", "boolean": "checkbox", "dropdown": "select",
            "picklist": "select", "textarea": "textarea"}.get(ftype, "text")


# ── SLARule ───────────────────────────────────────────────────────────────────

def _parse_sla(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None
    sla_name = root.get("name") or root.get("pyLabel") or name.split("/")[-1]
    goal     = _find_duration(root, "pyGoal",     24.0)
    deadline = _find_duration(root, "pyDeadline", 48.0)
    escalation_to = ""
    for el in root.iter():
        if _local(el.tag) in ("EscalationAction", "pyEscalationAction"):
            escalation_to = el.get("pyAssignTo") or el.get("target") or ""
            break
    return {
        "name":           sla_name,
        "rule_type":      "SLARule",
        "goal_hours":     goal,
        "deadline_hours": deadline,
        "escalation_to":  escalation_to[:200],
    }


def _find_duration(root, tag: str, default: float) -> float:
    for el in root.iter():
        if _local(el.tag) == tag:
            try:
                val  = float(el.get("pyValue") or el.text or default)
                unit = (el.get("pyUnit") or "hours").lower()
                if "minute" in unit:
                    return val / 60.0
                if "day" in unit:
                    return val * 24.0
                return val
            except (ValueError, TypeError):
                pass
    return default


# ── AccessGroup ───────────────────────────────────────────────────────────────

def _parse_access_group(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None
    ag_name = root.get("name") or root.get("pyLabel") or name.split("/")[-1]
    roles: list[str] = []
    for el in root.iter():
        if _local(el.tag) in ("Role", "pyRole"):
            role = el.get("name") or el.get("pyName") or (el.text or "").strip()
            if role:
                roles.append(role[:200])
    return {"name": ag_name, "rule_type": "AccessGroup", "roles": roles[:50]}


# ── DecisionTable → business rule ────────────────────────────────────────────

def _parse_decision_table(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None
    dt_name = root.get("name") or root.get("pyLabel") or name.split("/")[-1]
    conditions: list[dict] = []
    for el in root.iter():
        if _local(el.tag) in ("Row", "pyRow", "Condition", "DecisionRow"):
            cond   = (el.get("pyCondition") or el.get("condition") or "")[:500]
            result = (el.get("pyResult")    or el.get("result")    or "")[:500]
            if cond or result:
                conditions.append({"condition": cond, "result": result})
    return {"name": dt_name, "rule_type": "DecisionTable", "conditions": conditions[:100]}


# ── Correspondence → email template ──────────────────────────────────────────

def _parse_correspondence(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None
    corr_name = root.get("name") or root.get("pyLabel") or name.split("/")[-1]
    subject = ""
    for el in root.iter():
        if _local(el.tag) in ("Subject", "pySubject"):
            subject = (el.text or "").strip()[:500]
            break
    return {"name": corr_name, "rule_type": "Correspondence", "subject": subject, "action_type": "email"}


# ── DataPage → data model ─────────────────────────────────────────────────────

def _parse_data_page(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None
    dp_name = root.get("name") or root.get("pyLabel") or name.split("/")[-1]
    fields: list[dict] = []
    for el in root.iter():
        if _local(el.tag) in ("Property", "pyProperty", "DataField"):
            fname = el.get("name") or el.get("pyName") or ""
            ftype = (el.get("pyType") or el.get("type") or "string").lower()
            if fname:
                fields.append({"field_key": fname[:200], "label": fname[:500], "data_type": ftype[:50]})
    return {"name": dp_name, "rule_type": "DataPage", "fields": fields[:200]}


# ── Assignment → step ─────────────────────────────────────────────────────────

def _parse_assignment(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None
    assign_name   = root.get("name") or root.get("pyLabel") or name.split("/")[-1]
    workbasket    = root.get("pyWorkBasket") or root.get("pyWorkbasket") or ""
    form_action   = root.get("pyFlowAction") or root.get("pyFormAction") or ""
    return {
        "name":           assign_name,
        "rule_type":      "Assignment",
        "step_type":      "user_task",
        "form_key":       form_action[:200] if form_action else None,
        "assignee_type":  "queue" if workbasket else "user",
        "workbasket":     workbasket[:200],
    }


# ── DataTransform ─────────────────────────────────────────────────────────────

def _parse_data_transform(name: str, content: str) -> dict | None:
    root = _safe_parse(content)
    if root is None:
        return None
    dt_name = root.get("name") or name.split("/")[-1]
    mappings: list[dict] = []
    for el in root.iter():
        if _local(el.tag) in ("Row", "pyRow", "Mapping"):
            src = (el.get("pySource") or el.get("source") or "")[:200]
            tgt = (el.get("pyTarget") or el.get("target") or "")[:200]
            if src or tgt:
                mappings.append({"source": src, "target": tgt})
    return {"name": dt_name, "rule_type": "DataTransform", "mappings": mappings[:100]}
