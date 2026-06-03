"""BPMN 2.0 deep parser — covers all Tier 1 BPM vendors.

Supported vendors (auto-detected by namespace):
  Camunda 7/8, jBPM/Kogito, Flowable, IBM BAW/BPM, Oracle BPM, Bizagi, Bonitasoft

Key improvements over old camunda.py:
  - Uses defusedxml (SEC-1: XXE, billion-laughs protection)
  - Extracts sequenceFlow ordering (no more round-robin)
  - Extracts gateway conditions → step conditions
  - Extracts boundary events → SLA/error hints
  - Extracts laneSet/lane → access groups
  - Extracts subProcess → nested stages
  - Extracts form refs from camunda:formRef, flowable:formKey, activiti:formKey
  - Size limit check (SEC-1)
"""
from __future__ import annotations

import logging
from typing import Optional

import defusedxml.ElementTree as ET

from case_service.hxmigrate.security import MAX_XML_BYTES

logger = logging.getLogger(__name__)

# ── Namespace map ─────────────────────────────────────────────────────────────

_NS = {
    "bpmn":     "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmn2":    "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "camunda":  "http://camunda.org/schema/1.0/bpmn",
    "flowable": "http://flowable.org/bpmn",
    "activiti": "http://activiti.org/bpmn",
    "tns":      "http://www.jboss.org/drools",
    "icp":      "http://www.ibm.com/xmlns/prod/websphere/ibm-bpm/",
    "bonita":   "http://www.bonitasoft.org/ns/connector/6.0",
}

_STEP_TYPE_MAP = {
    "userTask":          "user_task",
    "serviceTask":       "automated",
    "scriptTask":        "automated",
    "businessRuleTask":  "automated",
    "sendTask":          "automated",
    "receiveTask":       "automated",
    "manualTask":        "user_task",
    "callActivity":      "subprocess",
    "subProcess":        "subprocess",
}

_GATEWAY_TYPES = {"exclusiveGateway", "inclusiveGateway", "parallelGateway", "eventBasedGateway"}
_BOUNDARY_TYPES = {"boundaryEvent"}


def parse_files(files: list[dict]) -> dict:
    """Entry point: parse all BpmnProcess files."""
    result: dict[str, list] = {}
    for f in files:
        if f.get("rule_type") != "BpmnProcess":
            continue
        content = f.get("content", "")
        # SEC-1: size check
        if len(content.encode("utf-8", errors="replace")) > MAX_XML_BYTES:
            logger.warning("BPMN file %s too large, skipping", f.get("name", "?"))
            continue
        try:
            parsed = _parse_bpmn(f["name"], content)
            if parsed:
                result.setdefault("BpmnProcess", []).append(parsed)
        except Exception as e:
            logger.warning("BPMN parse failed for %s: %s", f["name"], type(e).__name__)
    return result


def _parse_bpmn(name: str, content: str) -> dict | None:
    try:
        # SEC-1: defusedxml blocks XXE and entity expansion
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.warning("XML parse error in %s: %s", name, type(e).__name__)
        return None

    processes = _find_processes(root)
    if not processes:
        return None

    case_types = []
    for proc in processes:
        parsed = _parse_process(proc, root)
        if parsed:
            parsed["filename"] = name
            case_types.append(parsed)

    return {"processes": case_types, "filename": name}


def _find_processes(root) -> list:
    """Find all <process> elements regardless of namespace prefix."""
    result = []
    for el in root.iter():
        local = _local(el.tag)
        if local == "process":
            result.append(el)
    return result


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _ns_prefix(tag: str) -> str:
    """Return the namespace URI from a qualified tag."""
    if "}" in tag:
        return tag.split("}")[0].lstrip("{")
    return ""


def _detect_vendor(root) -> str:
    """Detect vendor from namespace URIs present in the document."""
    ns_uris = set()
    for el in root.iter():
        ns_uris.add(_ns_prefix(el.tag))
        for attr in el.attrib:
            ns_uris.add(_ns_prefix(attr))

    if any("camunda" in u for u in ns_uris):
        return "camunda"
    if any("flowable" in u for u in ns_uris):
        return "flowable"
    if any("activiti" in u for u in ns_uris):
        return "activiti"
    if any("ibm" in u or "websphere" in u for u in ns_uris):
        return "ibm"
    if any("jboss" in u or "kogito" in u for u in ns_uris):
        return "jbpm"
    if any("bonitasoft" in u for u in ns_uris):
        return "bonitasoft"
    return "bpmn2"


def _parse_process(proc, root) -> dict | None:
    proc_name = proc.get("name") or proc.get("id") or "Imported Process"
    vendor    = _detect_vendor(root)

    # Index all elements by id for quick lookup
    elements: dict[str, object] = {}
    for el in proc.iter():
        eid = el.get("id")
        if eid:
            elements[eid] = el

    # ── Collect tasks, gateways, boundary events ─────────────────────────────
    tasks: dict[str, dict] = {}
    gateways: dict[str, dict] = {}
    boundary_events: list[dict] = []
    lanes: list[dict] = []

    for el in proc:
        local = _local(el.tag)
        eid   = el.get("id", "")
        ename = el.get("name") or eid

        if local in _STEP_TYPE_MAP:
            step_type = _STEP_TYPE_MAP[local]
            form_key  = _extract_form_ref(el, vendor)
            tasks[eid] = {
                "id":            eid,
                "name":          ename,
                "step_type":     step_type,
                "original_type": local,
                "form_key":      form_key,
                "order":         0,
            }

        elif local in _GATEWAY_TYPES:
            gateways[eid] = {
                "id":         eid,
                "name":       ename,
                "gateway_type": local,
                "conditions": [],
            }

        elif local == "boundaryEvent":
            boundary_events.append(_parse_boundary_event(el))

        elif local == "subProcess":
            # Treat subProcess as a stage containing its own tasks
            sub_tasks = _extract_subprocess_tasks(el, vendor)
            if sub_tasks:
                tasks[eid] = {
                    "id":        eid,
                    "name":      ename,
                    "step_type": "subprocess",
                    "original_type": "subProcess",
                    "form_key":  None,
                    "order":     0,
                    "sub_steps": sub_tasks,
                }

    # ── Collect sequence flows (ordering) ─────────────────────────────────────
    sequence_flows: list[dict] = []
    conditions_by_flow: dict[str, str] = {}

    for el in proc.iter():
        if _local(el.tag) == "sequenceFlow":
            src = el.get("sourceRef", "")
            tgt = el.get("targetRef", "")
            cond = _extract_condition(el)
            sequence_flows.append({"id": el.get("id", ""), "from": src, "to": tgt, "condition": cond})
            if cond:
                conditions_by_flow[el.get("id", "")] = cond

    # ── Topological sort of tasks using sequence flows ─────────────────────────
    ordered_task_ids = _topological_sort(list(tasks.keys()), sequence_flows)

    # Assign order and attach gateway conditions to downstream steps
    gateway_cond_map: dict[str, list[str]] = {}  # task_id → conditions from upstream gateways
    for flow in sequence_flows:
        src = flow["from"]
        tgt = flow["to"]
        if src in gateways and tgt in tasks and flow.get("condition"):
            gateway_cond_map.setdefault(tgt, []).append(flow["condition"])

    for i, tid in enumerate(ordered_task_ids):
        if tid in tasks:
            tasks[tid]["order"] = i
            if tid in gateway_cond_map:
                tasks[tid]["conditions"] = gateway_cond_map[tid]

    # ── Extract laneSet → access groups ──────────────────────────────────────
    access_groups: list[dict] = []
    for el in proc.iter():
        if _local(el.tag) == "laneSet":
            for lane in el:
                if _local(lane.tag) == "lane":
                    lane_name = lane.get("name") or lane.get("id") or "Lane"
                    # collect task refs in this lane
                    refs = [
                        c.text.strip()
                        for c in lane
                        if _local(c.tag) == "flowNodeRef" and c.text
                    ]
                    access_groups.append({
                        "name":      lane_name,
                        "task_refs": refs,
                    })

    # ── Extract SLA hints from boundary events ────────────────────────────────
    slas: list[dict] = []
    for be in boundary_events:
        if be["event_type"] == "timerBoundaryEvent":
            slas.append({
                "name":      f"SLA: {be.get('attached_to_name', be['id'])}",
                "source":    "boundaryEvent",
                "task_id":   be.get("attached_to"),
                "timer_def": be.get("timer_def", ""),
                "confidence": 0.6,
            })

    # ── Build stages from subProcesses or default single stage ───────────────
    stage_elements = [t for t in tasks.values() if t.get("original_type") == "subProcess"]
    regular_tasks  = [t for t in tasks.values() if t.get("original_type") != "subProcess"]

    if stage_elements:
        stages = []
        for sp in sorted(stage_elements, key=lambda x: x["order"]):
            stages.append({
                "id":    sp["id"],
                "name":  sp["name"],
                "steps": sorted(sp.get("sub_steps", []), key=lambda x: x.get("order", 0)),
            })
        # Any tasks outside subProcesses go into a final "Main" stage
        if regular_tasks:
            stages.append({
                "id":    "main",
                "name":  proc_name,
                "steps": sorted(regular_tasks, key=lambda x: x["order"]),
            })
    else:
        stages = [{
            "id":    "main",
            "name":  proc_name,
            "steps": sorted(regular_tasks, key=lambda x: x["order"]),
        }]

    return {
        "name":          proc_name,
        "rule_type":     "BpmnProcess",
        "vendor":        vendor,
        "stages":        stages,
        "access_groups": access_groups,
        "sla_hints":     slas,
        "process_id":    proc.get("id", ""),
    }


def _extract_form_ref(el, vendor: str) -> str | None:
    """Extract form reference from vendor extension elements."""
    # camunda:formRef attribute
    for attr, val in el.attrib.items():
        local = _local(attr)
        if local in ("formRef", "formKey"):
            return val[:200] if val else None

    # Check extensionElements for formData
    for child in el:
        if _local(child.tag) in ("extensionElements", "extensions"):
            for ext in child.iter():
                local = _local(ext.tag)
                if local in ("formData", "formProperty"):
                    fid = ext.get("formRef") or ext.get("id") or ext.get("key")
                    if fid:
                        return fid[:200]
    return None


def _extract_condition(el) -> str | None:
    """Extract condition expression from a sequenceFlow."""
    for child in el:
        if _local(child.tag) == "conditionExpression":
            text = (child.text or "").strip()
            return text[:500] if text else None
    # Also check attribute
    cond = el.get("condition") or el.get("conditionExpression")
    return cond[:500] if cond else None


def _parse_boundary_event(el) -> dict:
    eid = el.get("id", "")
    attached = el.get("attachedToRef", "")
    event_type = "boundaryEvent"
    timer_def  = ""
    for child in el.iter():
        local = _local(child.tag)
        if local == "timerEventDefinition":
            event_type = "timerBoundaryEvent"
            for td in child:
                timer_def = (td.text or "").strip()[:200]
        elif local == "errorEventDefinition":
            event_type = "errorBoundaryEvent"
        elif local == "escalationEventDefinition":
            event_type = "escalationBoundaryEvent"
    return {
        "id":          eid,
        "event_type":  event_type,
        "attached_to": attached,
        "timer_def":   timer_def,
    }


def _extract_subprocess_tasks(el, vendor: str) -> list[dict]:
    """Extract tasks from a subProcess element."""
    tasks = []
    for child in el:
        local = _local(child.tag)
        if local in _STEP_TYPE_MAP:
            tasks.append({
                "id":        child.get("id", ""),
                "name":      child.get("name") or child.get("id") or local,
                "step_type": _STEP_TYPE_MAP[local],
                "form_key":  _extract_form_ref(child, vendor),
                "order":     0,
                "conditions": [],
            })
    return tasks


def _topological_sort(task_ids: list[str], flows: list[dict]) -> list[str]:
    """Topological sort of tasks using sequence flows (Kahn's algorithm).

    Falls back to input order if cycles are detected or on any error.
    """
    try:
        from collections import deque
        in_degree: dict[str, int] = {t: 0 for t in task_ids}
        successors: dict[str, list[str]] = {t: [] for t in task_ids}

        for flow in flows:
            src, tgt = flow["from"], flow["to"]
            if src in in_degree and tgt in in_degree:
                in_degree[tgt] += 1
                successors[src].append(tgt)

        queue = deque(t for t in task_ids if in_degree[t] == 0)
        result: list[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for nxt in successors.get(node, []):
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        # Add any remaining (cycle members) in original order
        remaining = [t for t in task_ids if t not in set(result)]
        result.extend(remaining)
        return result

    except Exception:
        return task_ids
