"""
Tests for helix_engine.compiler
================================

End-to-end tests for the compiler pipeline:
  BPMN XML → Parser → Validator → Optimizer → CompilationResult

Run with:  pytest engine/tests/unit/test_compiler.py -v
"""

from __future__ import annotations

import pytest

from helix_ir.models.process import (
    EndEvent,
    ExclusiveGateway,
    ParallelGateway,
    ServiceTask,
    StartEvent,
    UserTask,
)
from helix_engine.compiler.compiler import BPMNCompiler, CompilationError, CompilationResult
from helix_engine.compiler.parser import BPMNParser, ParseError
from helix_engine.compiler.validator import Validator
from helix_engine.compiler.optimizer import Optimizer


# ── Test BPMN XML fixtures ────────────────────────────────────────────

SIMPLE_PROCESS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="simple" name="Simple Process" isExecutable="true">
    <startEvent id="start" name="Begin"/>
    <sequenceFlow id="f1" sourceRef="start" targetRef="task1"/>
    <serviceTask id="task1" name="Do Work" implementation="helix://worker/do"/>
    <sequenceFlow id="f2" sourceRef="task1" targetRef="end"/>
    <endEvent id="end" name="Finish"/>
  </process>
</definitions>
"""

ORDER_PROCESS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="order_process" name="Order Fulfillment" isExecutable="true">
    <startEvent id="start" name="Order Received"/>
    <sequenceFlow id="f1" sourceRef="start" targetRef="validate"/>

    <serviceTask id="validate" name="Validate Order"
                 implementation="helix://order-service/validate"/>
    <sequenceFlow id="f2" sourceRef="validate" targetRef="check_stock"/>

    <exclusiveGateway id="check_stock" name="In Stock?" default="f4"/>
    <sequenceFlow id="f3" sourceRef="check_stock" targetRef="ship">
      <conditionExpression>in_stock == True</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="f4" sourceRef="check_stock" targetRef="backorder"/>

    <serviceTask id="ship" name="Ship Order"
                 implementation="helix://logistics/ship"/>
    <sequenceFlow id="f5" sourceRef="ship" targetRef="notify"/>

    <userTask id="backorder" name="Handle Backorder" formKey="forms/backorder"/>
    <sequenceFlow id="f6" sourceRef="backorder" targetRef="notify"/>

    <parallelGateway id="notify" name="Notify All"/>
    <sequenceFlow id="f7" sourceRef="notify" targetRef="email"/>
    <sequenceFlow id="f8" sourceRef="notify" targetRef="crm"/>

    <sendTask id="email" name="Email Customer"
              implementation="helix://notifications/email"/>
    <sequenceFlow id="f9" sourceRef="email" targetRef="end"/>

    <serviceTask id="crm" name="Update CRM"
                 implementation="helix://crm/update"/>
    <sequenceFlow id="f10" sourceRef="crm" targetRef="end"/>

    <endEvent id="end" name="Order Complete"/>
  </process>
</definitions>
"""

NO_START_EVENT_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="broken" name="Broken Process" isExecutable="true">
    <serviceTask id="task1" name="Orphan Task"/>
    <endEvent id="end"/>
  </process>
</definitions>
"""


# ── Parser tests ──────────────────────────────────────────────────────

class TestBPMNParser:
    """Test Phase 1: XML → BPMNProcess."""

    def test_parse_simple_process(self):
        parser = BPMNParser()
        processes = parser.parse(SIMPLE_PROCESS_XML)
        assert len(processes) == 1
        process = processes[0]
        assert process.id == "simple"
        assert process.name == "Simple Process"
        assert process.is_executable is True

    def test_parse_elements(self):
        parser = BPMNParser()
        process = parser.parse(SIMPLE_PROCESS_XML)[0]
        assert len(process.elements) == 3
        assert isinstance(process.elements["start"], StartEvent)
        assert isinstance(process.elements["task1"], ServiceTask)
        assert isinstance(process.elements["end"], EndEvent)

    def test_parse_flows(self):
        parser = BPMNParser()
        process = parser.parse(SIMPLE_PROCESS_XML)[0]
        assert len(process.flows) == 2
        assert process.flows["f1"].source_ref == "start"
        assert process.flows["f1"].target_ref == "task1"

    def test_parse_service_task_implementation(self):
        parser = BPMNParser()
        process = parser.parse(SIMPLE_PROCESS_XML)[0]
        task = process.elements["task1"]
        assert isinstance(task, ServiceTask)
        assert task.implementation == "helix://worker/do"

    def test_parse_user_task_form_key(self):
        parser = BPMNParser()
        process = parser.parse(ORDER_PROCESS_XML)[0]
        task = process.elements["backorder"]
        assert isinstance(task, UserTask)
        assert task.form_key == "forms/backorder"

    def test_parse_exclusive_gateway(self):
        parser = BPMNParser()
        process = parser.parse(ORDER_PROCESS_XML)[0]
        gw = process.elements["check_stock"]
        assert isinstance(gw, ExclusiveGateway)
        assert gw.default_flow == "f4"

    def test_parse_parallel_gateway(self):
        parser = BPMNParser()
        process = parser.parse(ORDER_PROCESS_XML)[0]
        gw = process.elements["notify"]
        assert isinstance(gw, ParallelGateway)

    def test_parse_condition_expression(self):
        parser = BPMNParser()
        process = parser.parse(ORDER_PROCESS_XML)[0]
        flow = process.flows["f3"]
        assert flow.condition == "in_stock == True"

    def test_parse_no_process_raises(self):
        parser = BPMNParser()
        with pytest.raises(ParseError, match="No <process> elements"):
            parser.parse('<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"/>')

    def test_parse_order_process_element_count(self):
        parser = BPMNParser()
        process = parser.parse(ORDER_PROCESS_XML)[0]
        assert len(process.elements) == 9  # start, validate, check_stock, ship, backorder, notify, email, crm, end
        assert len(process.flows) == 10


# ── Validator tests ───────────────────────────────────────────────────

class TestValidator:
    """Test Phase 2: Semantic validation."""

    def test_valid_process_passes(self):
        parser = BPMNParser()
        process = parser.parse(ORDER_PROCESS_XML)[0]
        result = Validator().validate(process)
        assert result.is_valid

    def test_no_start_event_is_error(self):
        parser = BPMNParser()
        process = parser.parse(NO_START_EVENT_XML)[0]
        result = Validator().validate(process)
        assert not result.is_valid
        assert any("no start event" in e for e in result.errors)

    def test_unreachable_node_is_warning(self):
        parser = BPMNParser()
        process = parser.parse(SIMPLE_PROCESS_XML)[0]
        # Add an orphan node
        process.elements["orphan"] = ServiceTask(id="orphan", name="Orphan")
        result = Validator().validate(process)
        assert result.is_valid  # Warnings don't fail validation
        assert any("unreachable" in w for w in result.warnings)


# ── Optimizer tests ───────────────────────────────────────────────────

class TestOptimizer:
    """Test Phase 3: AST optimization."""

    def test_removes_dead_paths(self):
        parser = BPMNParser()
        process = parser.parse(SIMPLE_PROCESS_XML)[0]
        # Add an orphan
        process.elements["dead"] = ServiceTask(id="dead", name="Dead Node")
        assert "dead" in process.elements

        report = Optimizer().optimize(process)
        assert "dead" not in process.elements
        assert "dead" in report.removed_elements

    def test_no_changes_on_clean_process(self):
        parser = BPMNParser()
        process = parser.parse(SIMPLE_PROCESS_XML)[0]
        report = Optimizer().optimize(process)
        assert not report.had_changes


# ── End-to-end compiler tests ─────────────────────────────────────────

class TestBPMNCompiler:
    """Test the full pipeline: XML → CompilationResult."""

    def test_compile_simple_process(self):
        compiler = BPMNCompiler()
        result = compiler.compile(SIMPLE_PROCESS_XML)
        assert isinstance(result, CompilationResult)
        assert result.process.id == "simple"
        assert result.validation.is_valid
        assert len(result.process.elements) == 3

    def test_compile_order_process(self):
        compiler = BPMNCompiler()
        result = compiler.compile(ORDER_PROCESS_XML)
        assert result.process.id == "order_process"
        assert result.validation.is_valid
        assert len(result.process.elements) == 9

    def test_compile_strict_rejects_invalid(self):
        compiler = BPMNCompiler(strict=True)
        with pytest.raises(CompilationError):
            compiler.compile(NO_START_EVENT_XML)

    def test_compile_non_strict_allows_invalid(self):
        compiler = BPMNCompiler(strict=False)
        result = compiler.compile(NO_START_EVENT_XML)
        assert not result.validation.is_valid
        assert len(result.validation.errors) > 0

    def test_compile_preserves_graph_traversal(self):
        compiler = BPMNCompiler()
        result = compiler.compile(ORDER_PROCESS_XML)
        process = result.process

        # Verify the graph is navigable
        succs = process.successors("check_stock")
        succ_ids = {s.id for s in succs}
        assert "ship" in succ_ids
        assert "backorder" in succ_ids

        preds = process.predecessors("end")
        pred_ids = {p.id for p in preds}
        assert "email" in pred_ids
        assert "crm" in pred_ids

    def test_compile_all(self):
        compiler = BPMNCompiler()
        results = compiler.compile_all(ORDER_PROCESS_XML)
        assert len(results) == 1
        assert results[0].process.id == "order_process"

    def test_compile_invalid_process_index(self):
        compiler = BPMNCompiler()
        with pytest.raises(CompilationError, match="process index"):
            compiler.compile(SIMPLE_PROCESS_XML, process_index=5)
