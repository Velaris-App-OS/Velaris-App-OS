"""Minimal BPMN 2.0 XML parser.

Parses the structural graph of a BPMN process definition: nodes and
sequence flows.  No BPMN engine library dependency — uses stdlib xml.etree.
"""
from __future__ import annotations

import ast
import re
import defusedxml.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

# AST node types allowed in BPMN condition expressions.
# Call, Attribute, Subscript, Import etc. are absent — any such node in a
# parsed expression is rejected before eval() is ever called.
_ALLOWED_EXPR_NODES = frozenset({
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not,
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Name, ast.Constant,
    ast.Load,   # context node on every Name — must be allowed
})

# BPMN 2.0 namespaces
_NS = {
    "bpmn":  "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmn2": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "camunda": "http://camunda.org/schema/1.0/bpmn",
}

# Tag → canonical node_type
_TYPE_MAP = {
    "startEvent":           "startEvent",
    "endEvent":             "endEvent",
    "serviceTask":          "serviceTask",
    "userTask":             "userTask",
    "scriptTask":           "scriptTask",
    "callActivity":         "spawnCase",   # BPMN standard → Helix-native name (avoids clash with Pega "Activity")
    "exclusiveGateway":     "exclusiveGateway",
    "parallelGateway":      "parallelGateway",
    "inclusiveGateway":     "inclusiveGateway",
    "eventBasedGateway":    "eventBasedGateway",
    "subProcess":           "subProcess",
    "boundaryEvent":        "boundaryEvent",
    "intermediateCatchEvent": "intermediateCatchEvent",
    "intermediateThrowEvent": "intermediateThrowEvent",
}


@dataclass
class BpmnNode:
    id: str
    node_type: str
    name: str | None
    outgoing: list[str] = field(default_factory=list)
    incoming: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class BpmnFlow:
    id: str
    source: str
    target: str
    condition: str | None = None
    name: str | None = None


@dataclass
class BpmnProcess:
    id: str
    name: str | None
    nodes: dict[str, BpmnNode]
    flows: dict[str, BpmnFlow]
    start_events: list[str]

    def next_nodes(self, node_id: str, context: dict) -> list[str]:
        """Return the IDs of successor nodes from *node_id*.

        For exclusive gateways the first condition that evaluates truthy wins.
        For parallel gateways all outgoing flows are returned.
        For everything else the single outgoing flow is followed.
        """
        node = self.nodes.get(node_id)
        if not node:
            return []

        if node.node_type == "exclusiveGateway":
            default_target: str | None = None
            for flow_id in node.outgoing:
                flow = self.flows.get(flow_id)
                if not flow:
                    continue
                if flow.condition:
                    if _eval_condition(flow.condition, context):
                        return [flow.target]
                else:
                    default_target = flow.target
            return [default_target] if default_target else []

        targets = []
        for flow_id in node.outgoing:
            flow = self.flows.get(flow_id)
            if flow:
                targets.append(flow.target)
        return targets


def parse(bpmn_xml: str) -> BpmnProcess:
    """Parse a BPMN 2.0 XML string and return a BpmnProcess."""
    root = ET.fromstring(bpmn_xml)

    # Locate the first <process> element (handles both namespaced and plain)
    process_el = None
    for tag in ("process", "{http://www.omg.org/spec/BPMN/20100524/MODEL}process"):
        process_el = root.find(f".//{tag}")
        if process_el is not None:
            break
    if process_el is None:
        raise ValueError("No <process> element found in BPMN XML")

    nodes: dict[str, BpmnNode] = {}
    flows: dict[str, BpmnFlow] = {}

    def _strip_ns(tag: str) -> str:
        return re.sub(r"\{[^}]+\}", "", tag)

    for el in process_el:
        local = _strip_ns(el.tag)
        node_type = _TYPE_MAP.get(local)
        if node_type:
            node_id = el.get("id", "")
            # Collect extensions (e.g. connector type from Camunda)
            extensions: dict[str, Any] = {}
            ext_el = el.find(".//{http://camunda.org/schema/1.0/bpmn}properties")
            if ext_el is not None:
                for prop in ext_el:
                    pname = prop.get("name", "")
                    pval  = prop.get("value", "")
                    if pname:
                        extensions[pname] = pval
            # Also grab plain camunda:property values
            for prop in el.iter("{http://camunda.org/schema/1.0/bpmn}property"):
                pname = prop.get("name", "")
                pval  = prop.get("value", "")
                if pname:
                    extensions[pname] = pval

            nodes[node_id] = BpmnNode(
                id=node_id,
                node_type=node_type,
                name=el.get("name"),
                extensions=extensions,
            )

        elif local == "sequenceFlow":
            flow_id  = el.get("id", "")
            source   = el.get("sourceRef", "")
            target   = el.get("targetRef", "")
            cond_el  = el.find(
                "{http://www.omg.org/spec/BPMN/20100524/MODEL}conditionExpression"
            )
            if cond_el is None:
                cond_el = el.find("conditionExpression")
            condition = (cond_el.text or "").strip() if cond_el is not None else None
            flows[flow_id] = BpmnFlow(
                id=flow_id, source=source, target=target,
                condition=condition or None, name=el.get("name"),
            )
            if source in nodes:
                nodes[source].outgoing.append(flow_id)
            if target in nodes:
                nodes[target].incoming.append(flow_id)

    start_events = [n.id for n in nodes.values() if n.node_type == "startEvent"]
    return BpmnProcess(
        id=process_el.get("id", ""),
        name=process_el.get("name"),
        nodes=nodes,
        flows=flows,
        start_events=start_events,
    )


def _eval_condition(expr: str, context: dict) -> bool:
    """Evaluate a BPMN condition expression against context.

    Supports: ${field == 'value'}, ${field > 5}, ${field}, Python-style
    and BPMN/Groovy-style literals (true/false/null).

    Security: the expression is parsed to an AST and every node type is
    checked against _ALLOWED_EXPR_NODES before eval() is called. Any node
    that could cause side-effects (Call, Attribute, Subscript, Import, …)
    causes an immediate False return — eval() never runs.
    """
    stripped = re.sub(r"^\$\{(.+)\}$", r"\1", expr.strip())
    stripped = re.sub(r"\btrue\b",  "True",  stripped)
    stripped = re.sub(r"\bfalse\b", "False", stripped)
    stripped = re.sub(r"\bnull\b",  "None",  stripped)
    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_EXPR_NODES:
            return False
    try:
        return bool(eval(  # noqa: S307 — AST-validated; no calls/attrs/subscripts
            compile(tree, "<bpmn_condition>", "eval"),
            {"__builtins__": {}},
            context,
        ))
    except Exception:
        return False
