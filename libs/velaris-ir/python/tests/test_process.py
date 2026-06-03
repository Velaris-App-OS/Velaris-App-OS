"""
Tests for helix_ir.models.process
=================================

These tests verify the IR data structures — the shared vocabulary that
every Helix component depends on.  If these break, everything breaks.

Run with:  pytest libs/helix-ir/python/tests/test_process.py -v
"""

from __future__ import annotations

import pytest

from helix_ir.models.process import (
    BPMNProcess,
    BoundaryEvent,
    EndEvent,
    EventDefinition,
    EventType,
    ExclusiveGateway,
    GatewayDirection,
    MultiInstanceConfig,
    MultiInstanceType,
    ParallelGateway,
    ScriptTask,
    SequenceFlow,
    ServiceTask,
    StartEvent,
    SubProcess,
    UserTask,
)


# ── Helpers ───────────────────────────────────────────────────────────

def make_simple_process() -> BPMNProcess:
    """
    Build a minimal process for testing:

        start → validate → check_stock (XOR) → ship → end
                                              → backorder → end
    """
    process = BPMNProcess(id="test_process", name="Test Process")

    # Elements
    process.elements["start"] = StartEvent(id="start", name="Start", outgoing=["f1"])
    process.elements["validate"] = ServiceTask(
        id="validate", name="Validate Order",
        implementation="helix://order-service/validate",
        incoming=["f1"], outgoing=["f2"],
    )
    process.elements["check_stock"] = ExclusiveGateway(
        id="check_stock", name="In Stock?",
        direction=GatewayDirection.DIVERGING,
        incoming=["f2"], outgoing=["f3", "f4"],
        default_flow="f4",
    )
    process.elements["ship"] = ServiceTask(
        id="ship", name="Ship Order",
        implementation="helix://logistics/ship",
        incoming=["f3"], outgoing=["f5"],
    )
    process.elements["backorder"] = UserTask(
        id="backorder", name="Handle Backorder",
        form_key="forms/backorder",
        incoming=["f4"], outgoing=["f6"],
    )
    process.elements["end"] = EndEvent(id="end", name="Done", incoming=["f5", "f6"])

    # Flows
    process.flows["f1"] = SequenceFlow(id="f1", source_ref="start", target_ref="validate")
    process.flows["f2"] = SequenceFlow(id="f2", source_ref="validate", target_ref="check_stock")
    process.flows["f3"] = SequenceFlow(
        id="f3", source_ref="check_stock", target_ref="ship",
        condition="in_stock == True",
    )
    process.flows["f4"] = SequenceFlow(id="f4", source_ref="check_stock", target_ref="backorder")
    process.flows["f5"] = SequenceFlow(id="f5", source_ref="ship", target_ref="end")
    process.flows["f6"] = SequenceFlow(id="f6", source_ref="backorder", target_ref="end")

    return process


# ── Tests ─────────────────────────────────────────────────────────────

class TestBPMNProcess:
    """Test the BPMNProcess container and its convenience methods."""

    def test_start_events(self):
        process = make_simple_process()
        starts = process.start_events
        assert len(starts) == 1
        assert starts[0].id == "start"

    def test_end_events(self):
        process = make_simple_process()
        ends = process.end_events
        assert len(ends) == 1
        assert ends[0].id == "end"

    def test_elements_by_type(self):
        process = make_simple_process()
        service_tasks = process.elements_by_type(ServiceTask)
        assert len(service_tasks) == 2
        user_tasks = process.elements_by_type(UserTask)
        assert len(user_tasks) == 1
        gateways = process.elements_by_type(ExclusiveGateway)
        assert len(gateways) == 1

    def test_outgoing_flows(self):
        process = make_simple_process()
        flows = process.outgoing_flows("check_stock")
        assert len(flows) == 2
        assert {f.target_ref for f in flows} == {"ship", "backorder"}

    def test_incoming_flows(self):
        process = make_simple_process()
        flows = process.incoming_flows("end")
        assert len(flows) == 2

    def test_successors(self):
        process = make_simple_process()
        succs = process.successors("check_stock")
        assert len(succs) == 2
        succ_ids = {s.id for s in succs}
        assert succ_ids == {"ship", "backorder"}

    def test_predecessors(self):
        process = make_simple_process()
        preds = process.predecessors("check_stock")
        assert len(preds) == 1
        assert preds[0].id == "validate"

    def test_target_of(self):
        process = make_simple_process()
        target = process.target_of("f3")
        assert target is not None
        assert target.id == "ship"

    def test_target_of_nonexistent(self):
        process = make_simple_process()
        assert process.target_of("nonexistent") is None


class TestElementTypes:
    """Test that each element type carries its specific fields."""

    def test_user_task_fields(self):
        task = UserTask(
            id="ut1", name="Review",
            form_key="forms/review",
            assignee="manager",
            candidate_groups=["approvers"],
        )
        assert task.form_key == "forms/review"
        assert task.assignee == "manager"
        assert task.candidate_groups == ["approvers"]

    def test_service_task_implementation(self):
        task = ServiceTask(
            id="st1", name="Call API",
            implementation="helix://my-service/endpoint",
        )
        assert task.implementation == "helix://my-service/endpoint"

    def test_script_task_body(self):
        task = ScriptTask(
            id="sc1", name="Transform",
            language="python",
            script="result = input_value * 2",
        )
        assert task.language == "python"
        assert "input_value" in task.script

    def test_event_definition(self):
        defn = EventDefinition(
            type=EventType.TIMER,
            timer_value="PT30M",
        )
        assert defn.type == EventType.TIMER
        assert defn.timer_value == "PT30M"

    def test_multi_instance_config(self):
        mi = MultiInstanceConfig(
            type=MultiInstanceType.PARALLEL,
            collection="order_items",
            element_variable="item",
            completion_condition="approved_count >= 3",
        )
        assert mi.type == MultiInstanceType.PARALLEL
        assert mi.collection == "order_items"

    def test_boundary_event(self):
        evt = BoundaryEvent(
            id="be1",
            attached_to="long_task",
            interrupting=False,
            definitions=[EventDefinition(type=EventType.TIMER, timer_value="PT1H")],
        )
        assert evt.attached_to == "long_task"
        assert evt.interrupting is False
        assert evt.definitions[0].timer_value == "PT1H"

    def test_subprocess_body(self):
        inner = BPMNProcess(id="inner", name="Inner Process")
        inner.elements["inner_start"] = StartEvent(id="inner_start")
        inner.elements["inner_end"] = EndEvent(id="inner_end")

        sp = SubProcess(id="sp1", name="My Subprocess", body=inner)
        assert sp.body is not None
        assert len(sp.body.elements) == 2

    def test_gateway_direction(self):
        gw = ParallelGateway(
            id="pg1", name="Fork",
            direction=GatewayDirection.DIVERGING,
        )
        assert gw.direction == GatewayDirection.DIVERGING


class TestSequenceFlow:
    """Test sequence flow basics."""

    def test_conditional_flow(self):
        flow = SequenceFlow(
            id="f1", source_ref="a", target_ref="b",
            condition="amount > 1000",
        )
        assert flow.condition == "amount > 1000"

    def test_unconditional_flow(self):
        flow = SequenceFlow(id="f2", source_ref="a", target_ref="b")
        assert flow.condition is None

    def test_named_flow(self):
        flow = SequenceFlow(
            id="f3", source_ref="a", target_ref="b",
            name="Yes",
        )
        assert flow.name == "Yes"
