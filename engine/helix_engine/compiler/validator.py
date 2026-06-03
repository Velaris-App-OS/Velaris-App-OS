"""
BPMN Process Validator
======================

Phase 2 of the compiler pipeline.

    BPMNProcess  →  ``Validator.validate()``  →  ValidationResult

This module catches structural and semantic issues BEFORE the process
reaches the runtime.  Fail fast, fail with clear messages.

Each ``_check_*`` method is an independent validation rule.  Adding a
new rule is one method + one line in ``validate()``.

Errors vs Warnings:
  - **Error**: The process cannot execute (e.g. no start event).
  - **Warning**: The process might behave unexpectedly (e.g. unreachable node).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from helix_ir.models.process import (
    BPMNProcess,
    BoundaryEvent,
    EndEvent,
    ExclusiveGateway,
    InclusiveGateway,
    ParallelGateway,
    ScriptTask,
    StartEvent,
    SubProcess,
)

ALLOWED_SCRIPT_LANGUAGES: frozenset[str] = frozenset({"python", "javascript", "groovy"})
MAX_SCRIPT_BYTES  = 65_536   # 64 KB hard limit — deploy rejected above this
WARN_SCRIPT_BYTES = 16_384   # 16 KB soft limit — warning issued above this

logger = structlog.get_logger()


@dataclass
class ValidationResult:
    """
    The output of validation — a list of errors and warnings.

    Usage::

        result = Validator().validate(process)
        if not result.is_valid:
            for error in result.errors:
                print(f"ERROR: {error}")
    """
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True if no errors were found (warnings are OK)."""
        return len(self.errors) == 0

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class Validator:
    """
    Validates a ``BPMNProcess`` for structural and semantic correctness.

    Usage::

        validator = Validator()
        result = validator.validate(process)
        if not result.is_valid:
            raise CompilationError(result.errors)
    """

    def validate(self, process: BPMNProcess) -> ValidationResult:
        """Run all validation rules against the process."""
        result = ValidationResult()

        # Each check is independent — order doesn't matter
        self._check_has_start_event(process, result)
        self._check_has_end_event(process, result)
        self._check_flow_references(process, result)
        self._check_exclusive_gateways(process, result)
        self._check_parallel_gateways(process, result)
        self._check_inclusive_gateways(process, result)
        self._check_boundary_events(process, result)
        self._check_reachability(process, result)
        self._check_subprocesses(process, result)
        self._check_script_tasks(process, result)

        if result.errors:
            logger.error("validation_failed", process=process.id,
                          error_count=len(result.errors))
        elif result.warnings:
            logger.warning("validation_warnings", process=process.id,
                            warning_count=len(result.warnings))
        else:
            logger.info("validation_passed", process=process.id)

        return result

    # ── Individual checks ─────────────────────────────────────────

    def _check_has_start_event(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Every executable process needs at least one start event."""
        if process.is_executable and not process.start_events:
            result.error(f"Process '{process.id}' has no start event")

    def _check_has_end_event(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Missing end events usually indicate an incomplete model."""
        if not process.end_events:
            result.warn(f"Process '{process.id}' has no end event")

    def _check_flow_references(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Every sequence flow must reference existing elements."""
        for flow in process.flows.values():
            if flow.source_ref not in process.elements:
                result.error(
                    f"Flow '{flow.id}' references unknown source '{flow.source_ref}'"
                )
            if flow.target_ref not in process.elements:
                result.error(
                    f"Flow '{flow.id}' references unknown target '{flow.target_ref}'"
                )

    def _check_exclusive_gateways(self, process: BPMNProcess, result: ValidationResult) -> None:
        """
        Exclusive gateways with multiple outgoing flows:
          - Each non-default flow should have a condition.
          - There should be a default flow.
        """
        for gw in process.elements_by_type(ExclusiveGateway):
            outgoing = process.outgoing_flows(gw.id)
            if len(outgoing) <= 1:
                continue

            for flow in outgoing:
                if flow.condition is None and flow.id != gw.default_flow:
                    result.warn(
                        f"Exclusive gateway '{gw.id}': outgoing flow '{flow.id}' "
                        f"has no condition and is not the default"
                    )

            if gw.default_flow is None:
                result.warn(f"Exclusive gateway '{gw.id}' has no default flow")

    def _check_parallel_gateways(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Parallel gateway outgoing flows should not have conditions."""
        for gw in process.elements_by_type(ParallelGateway):
            for flow in process.outgoing_flows(gw.id):
                if flow.condition is not None:
                    result.warn(
                        f"Parallel gateway '{gw.id}': flow '{flow.id}' has a condition "
                        f"(will be ignored — parallel gateways take ALL branches)"
                    )

    def _check_inclusive_gateways(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Inclusive gateways should have at least one conditioned flow or a default."""
        for gw in process.elements_by_type(InclusiveGateway):
            outgoing = process.outgoing_flows(gw.id)
            if len(outgoing) <= 1:
                continue

            has_condition = any(f.condition is not None for f in outgoing)
            if not has_condition and gw.default_flow is None:
                result.warn(
                    f"Inclusive gateway '{gw.id}' has no conditions and no default flow"
                )

    def _check_boundary_events(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Boundary events must reference an existing activity."""
        for evt in process.elements_by_type(BoundaryEvent):
            if evt.attached_to not in process.elements:
                result.error(
                    f"Boundary event '{evt.id}' is attached to unknown "
                    f"element '{evt.attached_to}'"
                )

    def _check_reachability(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Warn about nodes that can't be reached from any start event."""
        if not process.start_events:
            return  # Already reported as an error

        reachable: set[str] = set()
        queue = [se.id for se in process.start_events]

        while queue:
            current = queue.pop(0)
            if current in reachable:
                continue
            reachable.add(current)
            for successor in process.successors(current):
                queue.append(successor.id)

        for element_id in process.elements:
            element = process.elements[element_id]
            if element_id not in reachable and not isinstance(element, BoundaryEvent):
                result.warn(f"Element '{element_id}' is unreachable from start events")

    def _check_script_tasks(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Reject unsupported languages and oversized scripts at compile time."""
        for task in process.elements_by_type(ScriptTask):
            lang = (task.language or "").strip().lower()
            if lang not in ALLOWED_SCRIPT_LANGUAGES:
                result.error(
                    f"ScriptTask '{task.id}': language '{task.language}' is not allowed. "
                    f"Permitted values: {sorted(ALLOWED_SCRIPT_LANGUAGES)}"
                )
            script_bytes = len((task.script or "").encode("utf-8"))
            if script_bytes > MAX_SCRIPT_BYTES:
                result.error(
                    f"ScriptTask '{task.id}': script exceeds 64 KB hard limit "
                    f"({script_bytes} bytes). Move logic to a ServiceTask instead."
                )
            elif script_bytes > WARN_SCRIPT_BYTES:
                result.warn(
                    f"ScriptTask '{task.id}': script is {script_bytes} bytes (> 16 KB). "
                    f"Consider extracting into a ServiceTask for better maintainability."
                )

    def _check_subprocesses(self, process: BPMNProcess, result: ValidationResult) -> None:
        """Recursively validate embedded subprocesses."""
        for sp in process.elements_by_type(SubProcess):
            if sp.body is not None:
                sub_result = self.validate(sp.body)
                for err in sub_result.errors:
                    result.error(f"[subprocess '{sp.id}'] {err}")
                for warn in sub_result.warnings:
                    result.warn(f"[subprocess '{sp.id}'] {warn}")
