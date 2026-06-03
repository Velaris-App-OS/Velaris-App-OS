"""ServiceNow deep parser — full update set extraction.

Extracts from ServiceNow XML update sets:
  sys_hub_flow       → Flow Designer flow → process
  wf_workflow        → Legacy Workflow → process
  wf_stage           → workflow stage → stage
  wf_activity        → activity → step
  sys_hub_action_type_base → action → step
  sc_cat_item        → catalog item → form
  item_option_new    → catalog item variables → form fields
  sys_approval_rules → approval step
  sys_script_include → flagged as custom code (needs manual review)

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
        content = f.get("content", "")
        if not content.strip():
            continue
        # SEC-1: size check
        if len(content.encode("utf-8", errors="replace")) > MAX_XML_BYTES:
            logger.warning("ServiceNow file %s too large, skipping", f.get("name", "?"))
            continue
        try:
            parsed = _parse_update_set(f["name"], content)
            for key, items in parsed.items():
                result.setdefault(key, []).extend(items)
        except Exception as e:
            logger.warning("ServiceNow parse failed for %s: %s", f["name"], type(e).__name__)
    return result


def _safe_parse(content: str):
    try:
        return ET.fromstring(content)
    except Exception as e:
        logger.warning("XML parse error: %s", type(e).__name__)
        return None


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _child_text(el, name: str, default: str = "") -> str:
    child = el.find(name)
    return (getattr(child, "text", None) or default).strip()[:2000]


def _parse_update_set(filename: str, content: str) -> dict[str, list]:
    root = _safe_parse(content)
    if root is None:
        return {}

    result: dict[str, list] = {
        "Workflow":    [],
        "Catalog":     [],
        "BusinessRule": [],
        "CustomCode":  [],
    }

    # Index catalog items and their variables for form building
    catalog_items: dict[str, dict] = {}
    catalog_vars:  dict[str, list] = {}  # cat_item_id → [field_dicts]
    stage_map:     dict[str, dict] = {}  # stage sys_id → stage dict
    workflow_stages: dict[str, list] = {}  # workflow_id → [stage_dicts]
    workflow_steps:  dict[str, list] = {}  # workflow_id → [step_dicts]

    # ── First pass: collect all records ──────────────────────────────────────
    for record in root.iter():
        table = record.get("table") or _local(record.tag)

        if table == "sys_hub_flow":
            wf = _parse_flow_designer_flow(record)
            if wf:
                result["Workflow"].append(wf)

        elif table == "wf_workflow":
            wf = _parse_legacy_workflow(record)
            if wf:
                wf_id = _child_text(record, "sys_id") or record.get("sys_id") or ""
                if wf_id:
                    workflow_stages[wf_id] = []
                    workflow_steps[wf_id]  = []
                result["Workflow"].append(wf)

        elif table == "wf_stage":
            stage = _parse_wf_stage(record)
            if stage:
                wf_id = _child_text(record, "workflow") or ""
                stage_map[_child_text(record, "sys_id") or ""] = stage
                if wf_id not in workflow_stages:
                    workflow_stages[wf_id] = []
                workflow_stages[wf_id].append(stage)

        elif table == "wf_activity":
            step = _parse_wf_activity(record)
            if step:
                wf_id = _child_text(record, "workflow") or ""
                if wf_id not in workflow_steps:
                    workflow_steps[wf_id] = []
                workflow_steps[wf_id].append(step)

        elif table == "sys_hub_action_type_base":
            step = _parse_flow_action(record)
            if step:
                wf_id = _child_text(record, "flow") or ""
                workflow_steps.setdefault(wf_id, []).append(step)

        elif table == "sc_cat_item":
            cat = _parse_catalog_item(record)
            if cat:
                cat_id = _child_text(record, "sys_id") or record.get("sys_id") or ""
                catalog_items[cat_id] = cat

        elif table == "item_option_new":
            var = _parse_catalog_variable(record)
            if var:
                cat_id = _child_text(record, "cat_item") or ""
                catalog_vars.setdefault(cat_id, []).append(var)

        elif table in ("sys_script_include", "sys_ui_script"):
            name_val = _child_text(record, "name") or _child_text(record, "api_name") or "Script"
            result["CustomCode"].append({
                "name":      name_val,
                "rule_type": "CustomCode",
                "note":      "Custom script — needs manual review before migration",
            })

        elif table == "sys_approval_rules":
            step = _parse_approval_rule(record)
            if step:
                wf_id = _child_text(record, "workflow") or ""
                workflow_steps.setdefault(wf_id, []).append(step)

        elif table in ("sys_db_object", "sys_dictionary"):
            field = _parse_data_field(record)
            if field:
                result.setdefault("DataModel", []).append(field)

    # ── Second pass: attach stages/steps to workflows ─────────────────────────
    for wf in result["Workflow"]:
        wf_id = wf.get("sys_id", "")
        if wf_id in workflow_stages:
            stages = sorted(workflow_stages[wf_id], key=lambda s: s.get("order", 0))
            wf["stages"] = stages
        steps = workflow_steps.get(wf_id, [])
        _assign_steps_to_stages(wf, steps)

    # ── Third pass: build catalog item forms ─────────────────────────────────
    for cat_id, cat in catalog_items.items():
        fields = catalog_vars.get(cat_id, [])
        cat["fields"] = fields
        result["Catalog"].append(cat)

    # Clean up empty lists
    return {k: v for k, v in result.items() if v}


def _parse_flow_designer_flow(record) -> dict | None:
    name  = _child_text(record, "name") or _child_text(record, "internal_name") or "Flow"
    sys_id = _child_text(record, "sys_id") or ""
    desc  = _child_text(record, "description") or ""
    return {
        "name":      name,
        "rule_type": "Workflow",
        "source":    "Flow Designer",
        "sys_id":    sys_id,
        "description": desc[:500],
        "stages":    [],
        "steps":     [],
    }


def _parse_legacy_workflow(record) -> dict | None:
    name   = _child_text(record, "name") or "Workflow"
    sys_id = _child_text(record, "sys_id") or ""
    return {
        "name":      name,
        "rule_type": "Workflow",
        "source":    "Legacy Workflow",
        "sys_id":    sys_id,
        "stages":    [],
        "steps":     [],
    }


def _parse_wf_stage(record) -> dict | None:
    name  = _child_text(record, "name") or "Stage"
    sys_id = _child_text(record, "sys_id") or ""
    order = int(_child_text(record, "order") or "0")
    return {"id": sys_id, "name": name, "order": order, "steps": []}


def _parse_wf_activity(record) -> dict | None:
    name      = _child_text(record, "name") or "Activity"
    activity_type = _child_text(record, "type") or "user"
    stage_id  = _child_text(record, "stage") or ""
    sys_id    = _child_text(record, "sys_id") or ""
    order     = int(_child_text(record, "x") or "0")   # x-position used as rough order
    step_type = "user_task" if "user" in activity_type.lower() else "automated"
    return {
        "id":          sys_id,
        "name":        name,
        "step_type":   step_type,
        "stage_id":    stage_id,
        "order":       order,
        "form_key":    None,
        "assignee_type": "user",
    }


def _parse_flow_action(record) -> dict | None:
    name   = _child_text(record, "name") or "Action"
    sys_id = _child_text(record, "sys_id") or ""
    return {
        "id":        sys_id,
        "name":      name,
        "step_type": "automated",
        "stage_id":  "",
        "order":     0,
        "form_key":  None,
    }


def _parse_catalog_item(record) -> dict | None:
    name    = _child_text(record, "name") or "Catalog Item"
    sys_id  = _child_text(record, "sys_id") or ""
    desc    = _child_text(record, "description") or ""
    return {
        "name":        name,
        "rule_type":   "Catalog",
        "sys_id":      sys_id,
        "description": desc[:500],
        "fields":      [],
    }


def _parse_catalog_variable(record) -> dict | None:
    label    = _child_text(record, "question_text") or _child_text(record, "name") or "Field"
    var_name = _child_text(record, "name") or ""
    var_type = _child_text(record, "type") or "string"
    required = _child_text(record, "mandatory") == "true"
    if not var_name:
        return None
    return {
        "field_key":  var_name[:200],
        "label":      label[:500],
        "field_type": _sn_field_type(var_type),
        "required":   required,
    }


def _sn_field_type(sn_type: str) -> str:
    mapping = {
        "1":  "text",        # Single-line text
        "2":  "select",      # Choice
        "3":  "select",      # Multiple choice
        "4":  "textarea",    # Multi-line text
        "5":  "checkbox",    # Checkbox
        "6":  "select",      # Reference
        "7":  "date",        # Date
        "8":  "date",        # Date/time
        "9":  "number",      # Integer
        "10": "number",      # Decimal
        "16": "text",        # Email
        "17": "text",        # URL
        "26": "select",      # Lookup select box
    }
    return mapping.get(sn_type.strip(), "text")


def _parse_approval_rule(record) -> dict | None:
    name   = _child_text(record, "name") or "Approval"
    sys_id = _child_text(record, "sys_id") or ""
    return {
        "id":          sys_id,
        "name":        name,
        "step_type":   "approval",
        "stage_id":    "",
        "order":       0,
        "form_key":    None,
        "assignee_type": "user",
    }


def _parse_data_field(record) -> dict | None:
    col_name = _child_text(record, "element") or _child_text(record, "name") or ""
    label    = _child_text(record, "column_label") or col_name
    col_type = _child_text(record, "internal_type") or "string"
    if not col_name:
        return None
    return {
        "name":      col_name[:200],
        "rule_type": "DataField",
        "label":     label[:500],
        "data_type": col_type[:50],
    }


def _assign_steps_to_stages(wf: dict, steps: list[dict]) -> None:
    """Distribute steps into stages by stage_id reference."""
    stage_map = {s["id"]: s for s in wf.get("stages", [])}
    unassigned: list[dict] = []
    for step in sorted(steps, key=lambda s: s.get("order", 0)):
        sid = step.get("stage_id", "")
        if sid in stage_map:
            stage_map[sid].setdefault("steps", []).append(step)
        else:
            unassigned.append(step)

    if not wf.get("stages") and unassigned:
        wf["stages"] = [{"id": "main", "name": wf["name"], "order": 0, "steps": unassigned}]
    elif unassigned and wf.get("stages"):
        wf["stages"][-1].setdefault("steps", []).extend(unassigned)

    wf["steps"] = steps
