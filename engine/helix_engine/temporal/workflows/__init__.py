"""
Temporal Workflow — BPMN Process Execution
===========================================

This is the real ``@workflow.defn`` that Temporal executes durably.

It walks the compiled BPMN process graph and:
  - Dispatches tasks as Temporal activities (durable, retryable).
  - Evaluates gateway conditions in-workflow (deterministic).
  - Handles events in-workflow (timers, signals, messages).
  - Runs parallel branches via ``asyncio.gather``.
  - Executes subprocesses as child workflows.

Temporal determinism rules (IMPORTANT):
  - NO I/O in workflow code (no HTTP, no DB, no file reads).
  - NO ``datetime.now()`` — use ``workflow.now()`` instead.
  - NO ``random`` — use ``workflow.random()`` instead.
  - NO ``time.sleep()`` — use ``await workflow.sleep()`` instead.
  - All I/O goes through activities.

The workflow is re-entrant: Temporal replays it from event history
on recovery.  Every ``await`` on an activity or timer is a replay-safe
checkpoint.

Input:  ``WorkflowInput`` (process IR as dict + initial variables)
Output: ``WorkflowOutput`` (final variables + execution trace)
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

# Activities are imported for type registration only — Temporal resolves
# them by name string, not by Python reference.
with workflow.unsafe.imports_passed_through():
    import structlog
    from helix_ir.models.process import (
        BPMNProcess,
        BoundaryEvent,
        CallActivity,
        EndEvent,
        EventBasedGateway,
        EventType,
        ExclusiveGateway,
        InclusiveGateway,
        IntermediateCatchEvent,
        IntermediateThrowEvent,
        MultiInstanceType,
        ParallelGateway,
        StartEvent,
        SubProcess,
        _TaskBase,
    )
    from helix_engine.temporal.activities import ActivityInput, ActivityOutput
    from helix_engine.runtime.gateway import DefaultConditionEvaluator as _ConditionEvaluator

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════════
#  Workflow I/O — serialised by Temporal
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class WorkflowInput:
    """
    Input to the process workflow.

    ``process_data`` is the BPMNProcess serialised as a dict.
    We can't pass dataclass instances directly — Temporal needs
    JSON-serialisable data.
    """
    process_data: dict[str, Any]         # Serialised BPMNProcess
    variables: dict[str, Any] = field(default_factory=dict)
    business_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowInput:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class WorkflowOutput:
    """Output from a completed process workflow."""
    variables: dict[str, Any] = field(default_factory=dict)
    visited: list[str] = field(default_factory=list)
    status: str = "completed"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════
#  Process serialisation helpers
# ═══════════════════════════════════════════════════════════════════════
#
#  Temporal requires JSON-serialisable workflow inputs.  We serialise
#  the BPMNProcess IR to/from dicts.  This is intentionally simple —
#  a flat dict of elements keyed by id.

def _serialize_process(process: BPMNProcess) -> dict[str, Any]:
    """Convert a BPMNProcess to a JSON-safe dict for Temporal transport."""
    import enum
    import dataclasses

    def _make_json_safe(obj: Any) -> Any:
        """Recursively convert dataclasses and enums to JSON-safe types."""
        if isinstance(obj, enum.Enum):
            return obj.value
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {k: _make_json_safe(v) for k, v in dataclasses.asdict(obj).items()}
        if isinstance(obj, dict):
            return {k: _make_json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_make_json_safe(v) for v in obj]
        return obj

    elements = {}
    for eid, el in process.elements.items():
        el_dict = _make_json_safe(el) if hasattr(el, '__dataclass_fields__') else {}
        el_dict["_type"] = type(el).__name__
        elements[eid] = el_dict

    flows = {}
    for fid, flow in process.flows.items():
        flows[fid] = _make_json_safe(flow)

    return {
        "id": process.id,
        "name": process.name,
        "is_executable": process.is_executable,
        "elements": elements,
        "flows": flows,
    }
    

def _deserialize_process(data: dict[str, Any]) -> BPMNProcess:
    """Reconstruct a BPMNProcess from a serialised dict."""
    from helix_ir.models import process as models

    process = BPMNProcess(
        id=data["id"],
        name=data.get("name"),
        is_executable=data.get("is_executable", True),
    )

    # Reconstruct flows
    from helix_ir.models.process import SequenceFlow
    for fid, fdata in data.get("flows", {}).items():
        process.flows[fid] = SequenceFlow(**fdata)

    # Reconstruct elements by type name
    type_map = {
        "StartEvent": models.StartEvent,
        "EndEvent": models.EndEvent,
        "IntermediateCatchEvent": models.IntermediateCatchEvent,
        "IntermediateThrowEvent": models.IntermediateThrowEvent,
        "BoundaryEvent": models.BoundaryEvent,
        "UserTask": models.UserTask,
        "ServiceTask": models.ServiceTask,
        "ScriptTask": models.ScriptTask,
        "SendTask": models.SendTask,
        "ReceiveTask": models.ReceiveTask,
        "ManualTask": models.ManualTask,
        "BusinessRuleTask": models.BusinessRuleTask,
        "GenericTask": models.GenericTask,
        "ExclusiveGateway": models.ExclusiveGateway,
        "ParallelGateway": models.ParallelGateway,
        "InclusiveGateway": models.InclusiveGateway,
        "EventBasedGateway": models.EventBasedGateway,
        "SubProcess": models.SubProcess,
        "CallActivity": models.CallActivity,
    }

    for eid, edata in data.get("elements", {}).items():
        type_name = edata.pop("_type", "GenericTask")
        cls = type_map.get(type_name, models.GenericTask)

        # Filter to only fields the dataclass accepts
        valid_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in edata.items() if k in valid_fields}

        # Reconstruct nested dataclasses
        if "multi_instance" in filtered and isinstance(filtered["multi_instance"], dict):
            filtered["multi_instance"] = models.MultiInstanceConfig(**filtered["multi_instance"])
        if "definitions" in filtered and isinstance(filtered["definitions"], list):
            filtered["definitions"] = [
                models.EventDefinition(**d) if isinstance(d, dict) else d
                for d in filtered["definitions"]
            ]
        if "body" in filtered and isinstance(filtered["body"], dict):
            filtered["body"] = _deserialize_process(filtered["body"])

        try:
            process.elements[eid] = cls(**filtered)
        except TypeError:
            # Fallback: create with just id and name
            process.elements[eid] = cls(id=eid, name=edata.get("name"))

    return process


# ═══════════════════════════════════════════════════════════════════════
#  Activity type mapping
# ═══════════════════════════════════════════════════════════════════════

_TASK_ACTIVITY_MAP: dict[str, str] = {
    "UserTask":         "helix.task.user",
    "ServiceTask":      "helix.task.service",
    "ScriptTask":       "helix.task.script",
    "SendTask":         "helix.task.send",
    "ReceiveTask":      "helix.task.receive",
    "ManualTask":       "helix.task.generic",
    "BusinessRuleTask": "helix.task.business_rule",
    "GenericTask":      "helix.task.generic",
}

# Default retry policy for activities
_DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=3,
)


# ═══════════════════════════════════════════════════════════════════════
#  Condition evaluator (deterministic — safe for workflow replay)
# ═══════════════════════════════════════════════════════════════════════

def _evaluate_condition(expression: str, variables: dict[str, Any]) -> bool:
    """
    Evaluate a condition expression against process variables.

    Delegates to DefaultConditionEvaluator, which uses an AST whitelist
    to block all dangerous constructs before evaluation.
    """
    return _ConditionEvaluator().evaluate(expression, variables)


# ═══════════════════════════════════════════════════════════════════════
#  The workflow
# ═══════════════════════════════════════════════════════════════════════

@workflow.defn(name="helix.process")
class ProcessWorkflow:
    """
    Temporal workflow that executes a compiled BPMN process.

    Start it via the Temporal client::

        handle = await client.start_workflow(
            ProcessWorkflow.run,
            WorkflowInput(process_data=serialized, variables={"x": 1}).to_dict(),
            id="helix-order_process-abc123",
            task_queue="helix-engine",
        )

    Query current state::

        state = await handle.query(ProcessWorkflow.get_state)
    """

    def __init__(self) -> None:
        self._variables: dict[str, Any] = {}
        self._visited: list[str] = []
        self._status: str = "running"
        self._error: str | None = None
        self._pending_user_tasks: list[dict[str, Any]] = []
        self._completed_user_task_ids: set[str] = set()

    # ── Queries (read-only, called from outside) ──────────────────

    @workflow.query(name="get_state")
    def get_state(self) -> dict[str, Any]:
        """Query the current workflow state (variables, visited, status)."""
        return {
            "variables": self._variables,
            "visited": self._visited,
            "status": self._status,
            "error": self._error,
            "pending_user_tasks": list(self._pending_user_tasks),
        }

    @workflow.query(name="get_variables")
    def get_variables(self) -> dict[str, Any]:
        """Query just the current process variables."""
        return dict(self._variables)

    # ── Signals (external events sent into the workflow) ──────────

    @workflow.signal(name="user_task_completed")
    async def on_user_task_completed(self, data: dict[str, Any]) -> None:
        """
        Signal sent when a human completes a user task form.

        ``data`` should contain:
          - task_id: the BPMN task id
          - variables: form submission data
        """
        task_id = data.get("task_id", "")
        submitted_vars = data.get("variables", {})
        self._variables.update(submitted_vars)
        self._completed_user_task_ids.add(task_id)
        self._pending_user_tasks = [t for t in self._pending_user_tasks if t.get("task_id") != task_id]
        workflow.logger.info(f"User task completed: {task_id}")

    @workflow.signal(name="message_received")
    async def on_message_received(self, data: dict[str, Any]) -> None:
        """Signal sent when an external message arrives (for receive tasks)."""
        message_ref = data.get("message_ref", "")
        payload = data.get("variables", {})
        self._variables.update(payload)
        workflow.logger.info(f"Message received: {message_ref}")

    # ── Main workflow execution ───────────────────────────────────

    @workflow.run
    async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the BPMN process.

        This is the workflow entry point.  Temporal calls this and
        replays it on recovery.
        """
        wf_input = WorkflowInput.from_dict(input_data)
        process = _deserialize_process(wf_input.process_data)
        self._variables = dict(wf_input.variables)

        workflow.logger.info(f"Process started: {process.id}")

        try:
            # Execute from each start event
            for start in process.start_events:
                await self._execute_element(start.id, process)

            self._status = "completed"
            workflow.logger.info(
                f"Process completed: {process.id}, "
                f"steps: {len(self._visited)}"
            )

        except Exception as e:
            self._status = "failed"
            self._error = str(e)
            workflow.logger.error(f"Process failed: {process.id}, error: {e}")

        return WorkflowOutput(
            variables=self._variables,
            visited=self._visited,
            status=self._status,
            error=self._error,
        ).to_dict()

    # ── Element execution (the graph walker) ──────────────────────

    async def _execute_element(
        self,
        element_id: str,
        process: BPMNProcess,
    ) -> None:
        """Execute a single element and follow outgoing flows."""
        element = process.elements.get(element_id)
        if element is None:
            workflow.logger.warning(f"Element not found: {element_id}")
            return

        self._visited.append(element_id)
        type_name = type(element).__name__

        workflow.logger.info(
            f"Executing: {element_id} ({type_name}: {getattr(element, 'name', '')})"
        )

        match element:
            # ── Events ────────────────────────────────────────
            case StartEvent():
                pass  # Start events are entry points — nothing to do

            case EndEvent():
                await self._handle_end_event(element)
                return  # End of this path

            case IntermediateCatchEvent():
                await self._handle_intermediate_catch(element)

            case IntermediateThrowEvent():
                pass  # Throw events are fire-and-forget

            # ── Gateways ──────────────────────────────────────
            case ExclusiveGateway():
                await self._handle_exclusive_gateway(element, process)
                return  # Gateway handles its own continuation

            case ParallelGateway():
                await self._handle_parallel_gateway(element, process)
                return

            case InclusiveGateway():
                await self._handle_inclusive_gateway(element, process)
                return

            case EventBasedGateway():
                await self._handle_event_based_gateway(element, process)
                return

            # ── Containers ────────────────────────────────────
            case SubProcess() if element.body is not None:
                await self._handle_subprocess(element)

            case CallActivity():
                workflow.logger.warning(
                    f"CallActivity not yet implemented: {element.called_element}"
                )

            # ── Tasks (all types) ─────────────────────────────
            case _ if isinstance(element, _TaskBase):
                await self._handle_task(element)

        # Follow outgoing flows
        for flow in process.outgoing_flows(element_id):
            await self._execute_element(flow.target_ref, process)

    # ── Task handler (dispatches to Temporal activity) ────────────

    async def _handle_task(self, task: _TaskBase) -> None:
        """
        Execute a BPMN task as a Temporal activity.

        This is the core bridge: BPMN task → Temporal activity call.
        """
        type_name = type(task).__name__
        activity_name = _TASK_ACTIVITY_MAP.get(type_name, "helix.task.generic")

        # Build the activity input
        inp = ActivityInput(
            task_id=task.id,
            task_type=activity_name,
            task_name=task.name,
            variables=dict(self._variables),
            extensions=dict(task.extensions),
        )

        # Add task-type-specific fields
        if hasattr(task, "implementation"):
            inp.implementation = task.implementation
        if hasattr(task, "form_key"):
            inp.form_key = task.form_key
        if hasattr(task, "script"):
            inp.script_body = task.script
        if hasattr(task, "language"):
            inp.script_language = task.language
        if hasattr(task, "decision_ref"):
            inp.decision_ref = task.decision_ref
        if hasattr(task, "message_ref"):
            inp.message_ref = task.message_ref

        # Read timeout and retries from extensions
        timeout_secs = int(task.extensions.get("helix:timeout", 300))
        max_retries = int(task.extensions.get("helix:maxRetries", 3))
        task_queue = task.extensions.get("helix:taskQueue", "helix-engine")

        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=60),
            maximum_attempts=max_retries,
        )

        # Handle multi-instance
        mi = task.multi_instance
        if mi.type != MultiInstanceType.NONE and mi.collection:
            await self._handle_multi_instance_task(
                task, inp, activity_name, timeout_secs, retry_policy, task_queue
            )
            return

        # Execute the activity
        result = await workflow.execute_activity(
            activity_name,
            inp.to_dict(),
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=timeout_secs),
            retry_policy=retry_policy,
        )

        # Merge result variables
        if isinstance(result, dict) and "variables" in result:
            self._variables.update(result["variables"])

        # For user tasks, wait for the human to complete the form via signal
        if type_name == "UserTask":
            form_key = getattr(task, "form_key", None)
            pending = {"task_id": task.id, "task_name": task.name, "form_key": form_key}
            self._pending_user_tasks.append(pending)
            workflow.logger.info(f"Waiting for user task: {task.id} (form: {form_key})")
            task_id_capture = task.id
            await workflow.wait_condition(
                lambda: task_id_capture in self._completed_user_task_ids,
                timeout=timedelta(hours=72),
            )
            workflow.logger.info(f"User task resumed: {task.id}")

    async def _handle_multi_instance_task(
        self,
        task: _TaskBase,
        base_input: ActivityInput,
        activity_name: str,
        timeout_secs: int,
        retry_policy: RetryPolicy,
        task_queue: str,
    ) -> None:
        """Execute a multi-instance task (parallel or sequential)."""
        mi = task.multi_instance
        collection = self._variables.get(mi.collection, [])

        if not isinstance(collection, (list, tuple)):
            workflow.logger.warning(f"Multi-instance collection not iterable: {mi.collection}")
            return

        results: list[dict[str, Any]] = []

        if mi.type == MultiInstanceType.SEQUENTIAL:
            for item in collection:
                loop_input = ActivityInput.from_dict(base_input.to_dict())
                loop_input.variables = {**self._variables}
                if mi.element_variable:
                    loop_input.variables[mi.element_variable] = item

                result = await workflow.execute_activity(
                    activity_name,
                    loop_input.to_dict(),
                    task_queue=task_queue,
                    start_to_close_timeout=timedelta(seconds=timeout_secs),
                    retry_policy=retry_policy,
                )
                if isinstance(result, dict):
                    results.append(result.get("variables", {}))

        elif mi.type == MultiInstanceType.PARALLEL:
            async def _run_one(item: Any) -> dict[str, Any]:
                loop_input = ActivityInput.from_dict(base_input.to_dict())
                loop_input.variables = {**self._variables}
                if mi.element_variable:
                    loop_input.variables[mi.element_variable] = item

                r = await workflow.execute_activity(
                    activity_name,
                    loop_input.to_dict(),
                    task_queue=task_queue,
                    start_to_close_timeout=timedelta(seconds=timeout_secs),
                    retry_policy=retry_policy,
                )
                return r.get("variables", {}) if isinstance(r, dict) else {}

            results = list(await asyncio.gather(*[_run_one(item) for item in collection]))

        self._variables[f"_mi_results_{task.id}"] = results

    # ── Gateway handlers ──────────────────────────────────────────

    async def _handle_exclusive_gateway(
        self, gateway: ExclusiveGateway, process: BPMNProcess
    ) -> None:
        """Evaluate conditions, take exactly one branch."""
        outgoing = process.outgoing_flows(gateway.id)

        # Try conditioned flows
        for flow in outgoing:
            if flow.condition and flow.id != gateway.default_flow:
                if _evaluate_condition(flow.condition, self._variables):
                    workflow.logger.info(f"XOR gateway {gateway.id}: taking flow {flow.id}")
                    await self._execute_element(flow.target_ref, process)
                    return

        # Default flow
        if gateway.default_flow:
            default = process.flows.get(gateway.default_flow)
            if default:
                workflow.logger.info(f"XOR gateway {gateway.id}: taking default {default.id}")
                await self._execute_element(default.target_ref, process)
                return

        # Last resort
        if outgoing:
            await self._execute_element(outgoing[0].target_ref, process)

    async def _handle_parallel_gateway(
        self, gateway: ParallelGateway, process: BPMNProcess
    ) -> None:
        """Fork into ALL branches concurrently."""
        outgoing = process.outgoing_flows(gateway.id)

        if len(outgoing) <= 1:
            for flow in outgoing:
                await self._execute_element(flow.target_ref, process)
            return

        # Execute all branches in parallel
        workflow.logger.info(
            f"AND gateway {gateway.id}: forking into {len(outgoing)} branches"
        )
        await asyncio.gather(*[
            self._execute_element(flow.target_ref, process)
            for flow in outgoing
        ])

    async def _handle_inclusive_gateway(
        self, gateway: InclusiveGateway, process: BPMNProcess
    ) -> None:
        """Take all branches whose conditions are true."""
        outgoing = process.outgoing_flows(gateway.id)
        active: list[str] = []

        for flow in outgoing:
            if flow.id == gateway.default_flow:
                continue
            if flow.condition:
                if _evaluate_condition(flow.condition, self._variables):
                    active.append(flow.target_ref)
            else:
                active.append(flow.target_ref)

        if not active and gateway.default_flow:
            default = process.flows.get(gateway.default_flow)
            if default:
                active.append(default.target_ref)

        if len(active) <= 1:
            for target in active:
                await self._execute_element(target, process)
        else:
            await asyncio.gather(*[
                self._execute_element(target, process)
                for target in active
            ])

    async def _handle_event_based_gateway(
        self, gateway: EventBasedGateway, process: BPMNProcess
    ) -> None:
        """
        Wait for the first event to fire.

        Each outgoing flow leads to an intermediate catch event.
        We race them — whichever resolves first wins.
        """
        outgoing = process.outgoing_flows(gateway.id)

        if not outgoing:
            return

        # For now: take the first branch
        # TODO: Implement proper event racing with workflow.wait_condition
        workflow.logger.info(
            f"Event gateway {gateway.id}: {len(outgoing)} candidates "
            f"(taking first — racing not yet implemented)"
        )
        await self._execute_element(outgoing[0].target_ref, process)

    # ── Event handlers ────────────────────────────────────────────

    async def _handle_end_event(self, event: EndEvent) -> None:
        """Handle end event effects."""
        for defn in event.definitions:
            if defn.type == EventType.TERMINATE:
                workflow.logger.info(f"Terminate end event: {event.id}")
                # In a more complete implementation, this would cancel
                # all other running branches via CancellationScope
            elif defn.type == EventType.ERROR:
                workflow.logger.info(f"Error end event: {event.id} ({defn.error_ref})")
                raise RuntimeError(f"BPMN error: {defn.error_ref}")

    async def _handle_intermediate_catch(self, event: IntermediateCatchEvent) -> None:
        """Wait for an intermediate catch event to fire."""
        if not event.definitions:
            return

        defn = event.definitions[0]

        if defn.type == EventType.TIMER and defn.timer_value:
            duration = self._parse_iso_duration(defn.timer_value)
            workflow.logger.info(
                f"Timer catch: {event.id}, sleeping {duration.total_seconds()}s"
            )
            await asyncio.sleep(duration.total_seconds())

        elif defn.type == EventType.MESSAGE:
            workflow.logger.info(f"Message catch: {event.id}, waiting for signal")
            # Wait for the message_received signal
            # The signal handler updates self._variables
            await workflow.wait_condition(
                lambda: defn.message_ref in self._variables.get("_received_messages", []),
                timeout=timedelta(hours=24),
            )

        elif defn.type == EventType.SIGNAL:
            workflow.logger.info(f"Signal catch: {event.id}, waiting for signal")
            await workflow.wait_condition(
                lambda: defn.signal_ref in self._variables.get("_received_signals", []),
                timeout=timedelta(hours=24),
            )

    async def _handle_subprocess(self, sp: SubProcess) -> None:
        """Execute a subprocess as a child workflow."""
        if sp.body is None:
            return

        workflow.logger.info(f"Starting subprocess: {sp.id}")

        child_input = WorkflowInput(
            process_data=_serialize_process(sp.body),
            variables=dict(self._variables),
        )

        result = await workflow.execute_child_workflow(
            ProcessWorkflow.run,
            child_input.to_dict(),
            id=f"{workflow.info().workflow_id}-sub-{sp.id}",
            task_queue="helix-engine",
        )

        if isinstance(result, dict) and "variables" in result:
            self._variables.update(result["variables"])

        workflow.logger.info(f"Subprocess completed: {sp.id}")

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _parse_iso_duration(value: str) -> timedelta:
        """
        Parse a simple ISO 8601 duration to timedelta.

        Supports: PT30S, PT5M, PT1H, PT1H30M, P1D
        For complex durations, use a proper ISO parser.
        """
        import re
        total_seconds = 0.0

        # Day pattern
        day_match = re.search(r'(\d+)D', value)
        if day_match:
            total_seconds += int(day_match.group(1)) * 86400

        # Time components (after T)
        hour_match = re.search(r'(\d+)H', value)
        if hour_match:
            total_seconds += int(hour_match.group(1)) * 3600

        min_match = re.search(r'(\d+)M', value)
        if min_match:
            total_seconds += int(min_match.group(1)) * 60

        sec_match = re.search(r'(\d+)S', value)
        if sec_match:
            total_seconds += int(sec_match.group(1))

        return timedelta(seconds=total_seconds) if total_seconds > 0 else timedelta(seconds=60)
