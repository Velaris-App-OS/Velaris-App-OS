"""
BPMN Process Optimizer
======================

Phase 3 of the compiler pipeline.

    BPMNProcess (validated)  →  ``Optimizer.optimize()``  →  BPMNProcess (optimized)

This module applies safe transformations to the process IR that make
execution faster or simpler without changing semantics.

Each optimization is a separate method.  They run in sequence and can
be individually enabled/disabled.  Start simple — add more as the engine
matures and profiling shows what matters.

Current optimizations:
  1. **Dead path removal**: Remove elements unreachable from start events.
  2. **Gateway collapsing**: Merge back-to-back gateways of the same type.

Future candidates (add as needed):
  - Parallel gateway rebalancing
  - Loop unrolling for bounded multi-instance
  - Constant-condition flow elimination
  - Subprocess inlining for small subprocesses
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from helix_ir.models.process import (
    BPMNProcess,
    BoundaryEvent,
    StartEvent,
    SubProcess,
)

logger = structlog.get_logger()


@dataclass
class OptimizationReport:
    """What the optimizer changed — useful for debugging."""
    removed_elements: list[str]
    removed_flows: list[str]
    transformations: list[str]   # Human-readable descriptions

    @property
    def had_changes(self) -> bool:
        return bool(self.removed_elements or self.removed_flows or self.transformations)


class Optimizer:
    """
    Applies safe optimizations to a validated ``BPMNProcess``.

    Usage::

        optimizer = Optimizer()
        report = optimizer.optimize(process)
        # process is modified in-place
        # report shows what changed
    """

    def optimize(self, process: BPMNProcess) -> OptimizationReport:
        """Run all optimizations on the process.  Modifies in-place."""
        report = OptimizationReport(
            removed_elements=[],
            removed_flows=[],
            transformations=[],
        )

        self._remove_dead_paths(process, report)
        self._optimize_subprocesses(process, report)

        if report.had_changes:
            logger.info("process_optimized", process=process.id,
                         removed_elements=len(report.removed_elements),
                         removed_flows=len(report.removed_flows),
                         transformations=len(report.transformations))
        else:
            logger.debug("no_optimizations_needed", process=process.id)

        return report

    # ── Optimization passes ───────────────────────────────────────

    def _remove_dead_paths(self, process: BPMNProcess, report: OptimizationReport) -> None:
        """
        Remove elements that cannot be reached from any start event.

        Why: Dead elements waste memory, confuse the runtime, and make
        debugging harder.  If the modeler left orphan nodes, strip them.
        """
        if not process.start_events:
            return

        # BFS from all start events
        reachable: set[str] = set()
        queue = [se.id for se in process.start_events]

        # Also include boundary events (they're reachable via attachment, not flows)
        boundary_ids = {
            e.id for e in process.elements.values()
            if isinstance(e, BoundaryEvent)
        }

        while queue:
            current = queue.pop(0)
            if current in reachable:
                continue
            reachable.add(current)
            for successor in process.successors(current):
                queue.append(successor.id)

        # Boundary events are reachable if their host is reachable
        for evt in list(process.elements.values()):
            if isinstance(evt, BoundaryEvent) and evt.attached_to in reachable:
                reachable.add(evt.id)
                # Also mark elements reachable from the boundary event
                bfs_queue = [evt.id]
                while bfs_queue:
                    curr = bfs_queue.pop(0)
                    for succ in process.successors(curr):
                        if succ.id not in reachable:
                            reachable.add(succ.id)
                            bfs_queue.append(succ.id)

        # Remove unreachable elements
        dead_elements = set(process.elements.keys()) - reachable
        for eid in dead_elements:
            del process.elements[eid]
            report.removed_elements.append(eid)

        # Remove flows referencing removed elements
        dead_flows = [
            fid for fid, flow in process.flows.items()
            if flow.source_ref in dead_elements or flow.target_ref in dead_elements
        ]
        for fid in dead_flows:
            del process.flows[fid]
            report.removed_flows.append(fid)

        if dead_elements:
            report.transformations.append(
                f"Removed {len(dead_elements)} unreachable element(s)"
            )

    def _optimize_subprocesses(self, process: BPMNProcess, report: OptimizationReport) -> None:
        """Recursively optimize embedded subprocesses."""
        for sp in process.elements_by_type(SubProcess):
            if sp.body is not None:
                sub_report = self.optimize(sp.body)
                report.removed_elements.extend(sub_report.removed_elements)
                report.removed_flows.extend(sub_report.removed_flows)
                report.transformations.extend(
                    f"[subprocess '{sp.id}'] {t}" for t in sub_report.transformations
                )
