"""
BPMN 2.0 XML Parser
====================

Phase 1 of the compiler pipeline.

    BPMN XML string  →  ``BPMNParser.parse()``  →  list[BPMNProcess]

This module has ONE job: turn XML elements into helix-ir dataclasses.
No validation, no linking, no optimization — just faithful translation.

Each ``_parse_*`` method handles exactly one BPMN element type, making
it easy to find and fix parsing bugs for a specific element.

Namespace handling:
  BPMN 2.0 uses ``http://www.omg.org/spec/BPMN/20100524/MODEL`` as its
  XML namespace.  We strip it during tag comparison so the parser works
  with both namespaced (``<bpmn:startEvent>``) and bare (``<startEvent>``)
  XML files.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import structlog

from helix_ir.models.process import (
    BPMNProcess,
    BoundaryEvent,
    BusinessRuleTask,
    CallActivity,
    EndEvent,
    EventBasedGateway,
    EventDefinition,
    EventType,
    ExclusiveGateway,
    GatewayDirection,
    GenericTask,
    InclusiveGateway,
    IntermediateCatchEvent,
    IntermediateThrowEvent,
    ManualTask,
    MultiInstanceConfig,
    MultiInstanceType,
    ParallelGateway,
    ReceiveTask,
    ScriptTask,
    SendTask,
    SequenceFlow,
    ServiceTask,
    StartEvent,
    SubProcess,
    UserTask,
)

logger = structlog.get_logger()

# BPMN 2.0 XML namespace
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS = {"bpmn": BPMN_NS}


class ParseError(Exception):
    """Raised when the XML is structurally unparseable."""
    def __init__(self, message: str, element_id: str | None = None):
        self.element_id = element_id
        super().__init__(f"[{element_id}] {message}" if element_id else message)


# ═══════════════════════════════════════════════════════════════════════
#  Tag → parser dispatch table
# ═══════════════════════════════════════════════════════════════════════
#
#  Each key is the local tag name (without namespace prefix).
#  Each value is the method name on BPMNParser that handles it.
#  This table IS the set of supported BPMN elements.

_ELEMENT_PARSERS: dict[str, str] = {
    # Events
    "startEvent":               "_parse_start_event",
    "endEvent":                 "_parse_end_event",
    "intermediateCatchEvent":   "_parse_intermediate_catch",
    "intermediateThrowEvent":   "_parse_intermediate_throw",
    "boundaryEvent":            "_parse_boundary_event",
    # Tasks
    "task":                     "_parse_generic_task",
    "userTask":                 "_parse_user_task",
    "serviceTask":              "_parse_service_task",
    "scriptTask":               "_parse_script_task",
    "sendTask":                 "_parse_send_task",
    "receiveTask":              "_parse_receive_task",
    "manualTask":               "_parse_manual_task",
    "businessRuleTask":         "_parse_business_rule_task",
    # Gateways
    "exclusiveGateway":         "_parse_exclusive_gateway",
    "parallelGateway":          "_parse_parallel_gateway",
    "inclusiveGateway":         "_parse_inclusive_gateway",
    "eventBasedGateway":        "_parse_event_based_gateway",
    # Containers
    "subProcess":               "_parse_sub_process",
    "callActivity":             "_parse_call_activity",
}

# Event definition tag → EventType mapping
_EVENT_DEF_TAGS: dict[str, EventType] = {
    "timerEventDefinition":       EventType.TIMER,
    "messageEventDefinition":     EventType.MESSAGE,
    "signalEventDefinition":      EventType.SIGNAL,
    "errorEventDefinition":       EventType.ERROR,
    "escalationEventDefinition":  EventType.ESCALATION,
    "compensateEventDefinition":  EventType.COMPENSATION,
    "conditionalEventDefinition": EventType.CONDITIONAL,
    "terminateEventDefinition":   EventType.TERMINATE,
}


class BPMNParser:
    """
    Parses BPMN 2.0 XML into helix-ir ``BPMNProcess`` models.

    Usage::

        parser = BPMNParser()
        processes = parser.parse(xml_string)
        # processes[0].elements  → dict of all parsed elements
        # processes[0].flows     → dict of all sequence flows
    """

    def parse(self, xml_source: str | bytes) -> list[BPMNProcess]:
        """
        Parse a BPMN 2.0 XML document.

        Args:
            xml_source: Raw XML as a string or bytes.

        Returns:
            One ``BPMNProcess`` per ``<process>`` element in the document.

        Raises:
            ParseError: If the XML contains no ``<process>`` elements.
        """
        raw = xml_source if isinstance(xml_source, bytes) else xml_source.encode("utf-8")
        root = ET.fromstring(raw)
        processes: list[BPMNProcess] = []

        for proc_el in root.findall("bpmn:process", NS):
            processes.append(self._parse_process(proc_el))

        if not processes:
            raise ParseError("No <process> elements found in BPMN document")

        logger.info("bpmn_parsed", process_count=len(processes),
                     ids=[p.id for p in processes])
        return processes

    # ── Process-level parsing ─────────────────────────────────────

    def _parse_process(self, proc_el: ET.Element) -> BPMNProcess:
        """Parse a single <process> element and all its children."""
        process = BPMNProcess(
            id=proc_el.attrib.get("id", "unknown"),
            name=proc_el.attrib.get("name"),
            is_executable=proc_el.attrib.get("isExecutable", "false").lower() == "true",
        )

        # Sequence flows first (elements may reference them)
        for flow_el in proc_el.findall("bpmn:sequenceFlow", NS):
            flow = self._parse_sequence_flow(flow_el)
            process.flows[flow.id] = flow

        # All other elements via the dispatch table
        self._parse_children(proc_el, process)

        logger.debug("process_parsed", process_id=process.id,
                      elements=len(process.elements), flows=len(process.flows))
        return process

    def _parse_children(self, parent_el: ET.Element, process: BPMNProcess) -> None:
        """Walk child elements and dispatch to the appropriate parser method."""
        for child_el in parent_el:
            local_tag = self._local_tag(child_el)
            parser_method_name = _ELEMENT_PARSERS.get(local_tag)

            if parser_method_name is None:
                continue  # Skip non-element tags (sequenceFlow, dataObject, etc.)

            parser_method = getattr(self, parser_method_name)
            element = parser_method(child_el, process)
            process.elements[element.id] = element

    # ── Sequence flows ────────────────────────────────────────────

    def _parse_sequence_flow(self, el: ET.Element) -> SequenceFlow:
        condition = None
        cond_el = el.find("bpmn:conditionExpression", NS)
        if cond_el is not None and cond_el.text:
            condition = cond_el.text.strip()

        return SequenceFlow(
            id=el.attrib.get("id", ""),
            source_ref=el.attrib.get("sourceRef", ""),
            target_ref=el.attrib.get("targetRef", ""),
            name=el.attrib.get("name"),
            condition=condition,
        )

    # ── Event parsers ─────────────────────────────────────────────

    def _parse_start_event(self, el: ET.Element, process: BPMNProcess) -> StartEvent:
        return StartEvent(
            id=el.attrib.get("id", ""),
            name=el.attrib.get("name"),
            definitions=self._parse_event_definitions(el),
            outgoing=self._outgoing_refs(el),
        )

    def _parse_end_event(self, el: ET.Element, process: BPMNProcess) -> EndEvent:
        return EndEvent(
            id=el.attrib.get("id", ""),
            name=el.attrib.get("name"),
            definitions=self._parse_event_definitions(el),
            incoming=self._incoming_refs(el),
        )

    def _parse_intermediate_catch(self, el: ET.Element, process: BPMNProcess) -> IntermediateCatchEvent:
        return IntermediateCatchEvent(
            id=el.attrib.get("id", ""),
            name=el.attrib.get("name"),
            definitions=self._parse_event_definitions(el),
            incoming=self._incoming_refs(el),
            outgoing=self._outgoing_refs(el),
        )

    def _parse_intermediate_throw(self, el: ET.Element, process: BPMNProcess) -> IntermediateThrowEvent:
        return IntermediateThrowEvent(
            id=el.attrib.get("id", ""),
            name=el.attrib.get("name"),
            definitions=self._parse_event_definitions(el),
            incoming=self._incoming_refs(el),
            outgoing=self._outgoing_refs(el),
        )

    def _parse_boundary_event(self, el: ET.Element, process: BPMNProcess) -> BoundaryEvent:
        return BoundaryEvent(
            id=el.attrib.get("id", ""),
            name=el.attrib.get("name"),
            attached_to=el.attrib.get("attachedToRef", ""),
            interrupting=el.attrib.get("cancelActivity", "true").lower() == "true",
            definitions=self._parse_event_definitions(el),
            outgoing=self._outgoing_refs(el),
        )

    # ── Task parsers ──────────────────────────────────────────────

    def _parse_generic_task(self, el: ET.Element, process: BPMNProcess) -> GenericTask:
        return GenericTask(**self._common_task_fields(el))

    def _parse_user_task(self, el: ET.Element, process: BPMNProcess) -> UserTask:
        return UserTask(
            **self._common_task_fields(el),
            form_key=el.attrib.get("formKey"),
            assignee=el.attrib.get("assignee"),
        )

    def _parse_service_task(self, el: ET.Element, process: BPMNProcess) -> ServiceTask:
        return ServiceTask(
            **self._common_task_fields(el),
            implementation=el.attrib.get("implementation"),
        )

    def _parse_script_task(self, el: ET.Element, process: BPMNProcess) -> ScriptTask:
        script_el = el.find("bpmn:script", NS)
        return ScriptTask(
            **self._common_task_fields(el),
            language=el.attrib.get("scriptFormat", "python"),
            script=script_el.text.strip() if script_el is not None and script_el.text else "",
        )

    def _parse_send_task(self, el: ET.Element, process: BPMNProcess) -> SendTask:
        return SendTask(
            **self._common_task_fields(el),
            implementation=el.attrib.get("implementation"),
            message_ref=el.attrib.get("messageRef"),
        )

    def _parse_receive_task(self, el: ET.Element, process: BPMNProcess) -> ReceiveTask:
        return ReceiveTask(
            **self._common_task_fields(el),
            message_ref=el.attrib.get("messageRef"),
        )

    def _parse_manual_task(self, el: ET.Element, process: BPMNProcess) -> ManualTask:
        return ManualTask(**self._common_task_fields(el))

    def _parse_business_rule_task(self, el: ET.Element, process: BPMNProcess) -> BusinessRuleTask:
        return BusinessRuleTask(
            **self._common_task_fields(el),
            decision_ref=el.attrib.get("decisionRef"),
        )

    # ── Gateway parsers ───────────────────────────────────────────

    def _parse_exclusive_gateway(self, el: ET.Element, process: BPMNProcess) -> ExclusiveGateway:
        return ExclusiveGateway(**self._common_gateway_fields(el))

    def _parse_parallel_gateway(self, el: ET.Element, process: BPMNProcess) -> ParallelGateway:
        return ParallelGateway(**self._common_gateway_fields(el))

    def _parse_inclusive_gateway(self, el: ET.Element, process: BPMNProcess) -> InclusiveGateway:
        return InclusiveGateway(**self._common_gateway_fields(el))

    def _parse_event_based_gateway(self, el: ET.Element, process: BPMNProcess) -> EventBasedGateway:
        return EventBasedGateway(**self._common_gateway_fields(el))

    # ── Container parsers ─────────────────────────────────────────

    def _parse_sub_process(self, el: ET.Element, process: BPMNProcess) -> SubProcess:
        # Create a nested BPMNProcess for the subprocess body
        sub_process_ir = BPMNProcess(
            id=el.attrib.get("id", "") + "_body",
            name=el.attrib.get("name"),
            is_executable=True,
        )
        # Parse subprocess flows
        for flow_el in el.findall("bpmn:sequenceFlow", NS):
            flow = self._parse_sequence_flow(flow_el)
            sub_process_ir.flows[flow.id] = flow
        # Parse subprocess elements (recursive)
        self._parse_children(el, sub_process_ir)

        return SubProcess(
            **self._common_task_fields(el),
            body=sub_process_ir,
        )

    def _parse_call_activity(self, el: ET.Element, process: BPMNProcess) -> CallActivity:
        return CallActivity(
            **self._common_task_fields(el),
            called_element=el.attrib.get("calledElement"),
        )

    # ── Shared helpers ────────────────────────────────────────────

    def _common_task_fields(self, el: ET.Element) -> dict[str, Any]:
        """Extract fields common to all task types."""
        return {
            "id": el.attrib.get("id", ""),
            "name": el.attrib.get("name"),
            "incoming": self._incoming_refs(el),
            "outgoing": self._outgoing_refs(el),
            "multi_instance": self._parse_multi_instance(el),
            "default_flow": el.attrib.get("default"),
            "extensions": self._parse_extensions(el),
        }

    def _common_gateway_fields(self, el: ET.Element) -> dict[str, Any]:
        """Extract fields common to all gateway types."""
        direction = GatewayDirection.UNSPECIFIED
        if "gatewayDirection" in el.attrib:
            try:
                direction = GatewayDirection(el.attrib["gatewayDirection"])
            except ValueError:
                pass

        return {
            "id": el.attrib.get("id", ""),
            "name": el.attrib.get("name"),
            "direction": direction,
            "incoming": self._incoming_refs(el),
            "outgoing": self._outgoing_refs(el),
            "default_flow": el.attrib.get("default"),
        }

    def _incoming_refs(self, el: ET.Element) -> list[str]:
        return [inc.text.strip() for inc in el.findall("bpmn:incoming", NS) if inc.text]

    def _outgoing_refs(self, el: ET.Element) -> list[str]:
        return [out.text.strip() for out in el.findall("bpmn:outgoing", NS) if out.text]

    def _parse_event_definitions(self, el: ET.Element) -> list[EventDefinition]:
        """Parse all event definition children of an event element."""
        defs: list[EventDefinition] = []
        for child in el:
            local_tag = self._local_tag(child)
            evt_type = _EVENT_DEF_TAGS.get(local_tag)
            if evt_type is None:
                continue

            evt_def = EventDefinition(type=evt_type)

            # Timer: look for timeDuration / timeDate / timeCycle
            if evt_type == EventType.TIMER:
                for tag in ("timeDuration", "timeDate", "timeCycle"):
                    timer_el = child.find(f"bpmn:{tag}", NS)
                    if timer_el is not None and timer_el.text:
                        evt_def.timer_value = timer_el.text.strip()
                        break

            # Refs
            evt_def.error_ref = child.attrib.get("errorRef")
            evt_def.message_ref = child.attrib.get("messageRef")
            evt_def.signal_ref = child.attrib.get("signalRef")

            defs.append(evt_def)
        return defs

    def _parse_multi_instance(self, el: ET.Element) -> MultiInstanceConfig:
        """Parse <multiInstanceLoopCharacteristics>."""
        mi_el = el.find("bpmn:multiInstanceLoopCharacteristics", NS)
        if mi_el is None:
            return MultiInstanceConfig()

        is_seq = mi_el.attrib.get("isSequential", "false").lower() == "true"

        collection = None
        data_ref = mi_el.find("bpmn:loopDataInputRef", NS)
        if data_ref is not None and data_ref.text:
            collection = data_ref.text.strip()

        completion = None
        comp_el = mi_el.find("bpmn:completionCondition", NS)
        if comp_el is not None and comp_el.text:
            completion = comp_el.text.strip()

        return MultiInstanceConfig(
            type=MultiInstanceType.SEQUENTIAL if is_seq else MultiInstanceType.PARALLEL,
            collection=collection,
            element_variable=mi_el.attrib.get("elementVariable"),
            completion_condition=completion,
        )

    def _parse_extensions(self, el: ET.Element) -> dict[str, Any]:
        """Parse <extensionElements> for helix-specific properties."""
        extensions: dict[str, Any] = {}
        ext_el = el.find("bpmn:extensionElements", NS)
        if ext_el is None:
            return extensions

        for prop_group in ext_el:
            for prop in prop_group:
                name = prop.attrib.get("name", "")
                value = prop.attrib.get("value", prop.text or "")
                if name:
                    extensions[name] = value
        return extensions

    @staticmethod
    def _local_tag(el: ET.Element) -> str:
        """Strip XML namespace prefix from a tag.  ``{ns}localName`` → ``localName``."""
        tag = el.tag
        return tag.split("}")[-1] if "}" in tag else tag
