"""Temporal workflow: CaseLifecycleWorkflow.

One workflow execution per case instance.  Manages stages, step
assignments, SLA child workflows, and responds to signals for
step completion, status changes, and data updates.

Flow:
  1. Load case type definition (stages, SLAs)
  2. Set status to "open"
  3. For each stage in order:
     a. Update case's current stage
     b. Start stage-level SLA (if configured)
     c. Create assignments for all steps
     d. Wait for all required steps to complete (via signals)
     e. Evaluate exit criteria
  4. Resolve the case when all stages are done

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from case_service.temporal.activities.stage_activities import (
        create_stage_assignments,
        evaluate_exit_criteria,
        load_case_type_definition,
        resolve_case,
        update_case_stage,
        update_case_status,
    )
    from case_service.temporal.activities.sla_activities import (
        start_sla_tracking, start_sla_v2_tracking,
    )
    from case_service.temporal.activities.notification_activities import (
        send_case_notification,
    )


# Default retry policy for activities
_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)

_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@workflow.defn(name="helix.case.lifecycle")
class CaseLifecycleWorkflow:
    """Temporal workflow that drives a case through its stage lifecycle.

    Start via::

        handle = await client.start_workflow(
            CaseLifecycleWorkflow.run,
            {"case_id": "...", "case_type_id": "..."},
            id="helix-case-<case_id>",
            task_queue="helix-case-service",
        )

    Signal step completion::

        await handle.signal(
            CaseLifecycleWorkflow.step_completed,
            {"step_id": "step-review", "completed_by": "user-1"}
        )
    """

    def __init__(self) -> None:
        self._case_id: str = ""
        self._case_type_id: str = ""
        self._status: str = "new"
        self._current_stage_id: str | None = None
        self._completed_steps: set[str] = set()
        self._stages: list[dict[str, Any]] = []
        self._sla_policies: list[dict[str, Any]] = []
        self._cancelled: bool = False

    # ── Queries ───────────────────────────────────────────────────

    @workflow.query(name="get_case_state")
    def get_case_state(self) -> dict[str, Any]:
        return {
            "case_id": self._case_id,
            "status": self._status,
            "current_stage_id": self._current_stage_id,
            "completed_steps": list(self._completed_steps),
            "total_stages": len(self._stages),
        }

    # ── Signals ───────────────────────────────────────────────────

    @workflow.signal(name="step_completed")
    async def step_completed(self, data: dict[str, Any]) -> None:
        """Signal that a step has been completed by a user or system."""
        step_id = data.get("step_id", "")
        completed_by = data.get("completed_by", "system")
        self._completed_steps.add(step_id)
        workflow.logger.info(
            f"Step completed: {step_id} by {completed_by} "
            f"(total: {len(self._completed_steps)})"
        )

    @workflow.signal(name="cancel_case")
    async def cancel_case(self, data: dict[str, Any]) -> None:
        """Signal to cancel the case lifecycle."""
        self._cancelled = True
        workflow.logger.info(f"Case cancel signal received: {self._case_id}")

    @workflow.signal(name="status_changed")
    async def status_changed(self, data: dict[str, Any]) -> None:
        """External status change notification."""
        new_status = data.get("status", "")
        if new_status in ("cancelled", "closed"):
            self._cancelled = True
        self._status = new_status

    # ── Main workflow ─────────────────────────────────────────────

    @workflow.run
    async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Execute the case lifecycle through all stages."""
        self._case_id = input_data["case_id"]
        self._case_type_id = input_data["case_type_id"]

        workflow.logger.info(
            f"Case lifecycle starting: {self._case_id} "
            f"(type: {self._case_type_id})"
        )

        try:
            # 1. Load case type definition
            definition = await workflow.execute_activity(
                load_case_type_definition,
                self._case_type_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
            )

            self._stages = definition.get("stages", [])
            self._sla_policies = definition.get("sla_policies", [])

            if not self._stages:
                workflow.logger.info(
                    f"No stages defined for case {self._case_id} — "
                    f"resolving immediately"
                )
                await self._set_status("open")
                await self._resolve()
                return self._result("completed")

            # 2. Open the case
            await self._set_status("open")

            # 3. Notify case started
            await workflow.execute_activity(
                send_case_notification,
                args=[self._case_id, "case_started", None, None],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
            )

            # 4. Execute stages in order
            sorted_stages = sorted(
                self._stages, key=lambda s: s.get("order", 0)
            )

            for stage in sorted_stages:
                if self._cancelled:
                    break
                await self._execute_stage(stage)

            # 5. Resolve if not cancelled
            if not self._cancelled:
                await self._resolve()
                return self._result("completed")
            else:
                return self._result("cancelled")

        except Exception as e:
            workflow.logger.error(
                f"Case lifecycle failed: {self._case_id} — {e}"
            )
            self._status = "failed"
            return self._result("failed", error=str(e))

    # ── Stage execution ───────────────────────────────────────────

    async def _execute_stage(self, stage: dict[str, Any]) -> None:
        """Execute a single stage: enter → assign → wait → exit."""
        stage_id = stage["id"]
        stage_name = stage.get("name", stage_id)
        steps = stage.get("steps", [])

        workflow.logger.info(
            f"Entering stage: {stage_name} ({stage_id}) "
            f"with {len(steps)} steps"
        )

        self._current_stage_id = stage_id

        # Update case stage in DB
        await workflow.execute_activity(
            update_case_stage,
            args=[self._case_id, stage_id],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Start stage SLA if configured
        sla_policy_id = stage.get("sla_policy_id")
        if sla_policy_id:
            sla_policy = next(
                (s for s in self._sla_policies if s["id"] == sla_policy_id),
                None,
            )
            if sla_policy:
                # P34b: policy opts into v2 via use_v2=true OR has escalation_tree_id
                use_v2 = bool(sla_policy.get("use_v2") or sla_policy.get("escalation_tree_id"))
                activity_fn = start_sla_v2_tracking if use_v2 else start_sla_tracking
                await workflow.execute_activity(
                    activity_fn,
                    args=[self._case_id, sla_policy_id, stage_id],
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=_RETRY,
                )

        # Create assignments for all steps
        if steps:
            await workflow.execute_activity(
                create_stage_assignments,
                args=[self._case_id, stage_id, steps],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
            )

        # Wait for required steps to complete
        required_step_ids = {
            s["id"] for s in steps if s.get("required", True)
        }

        if required_step_ids:
            workflow.logger.info(
                f"Waiting for {len(required_step_ids)} required steps: "
                f"{required_step_ids}"
            )

            # Wait with a timeout of 7 days per stage
            try:
                await workflow.wait_condition(
                    lambda: required_step_ids.issubset(self._completed_steps)
                    or self._cancelled,
                    timeout=timedelta(days=7),
                )
            except TimeoutError:
                workflow.logger.warning(
                    f"Stage {stage_id} timed out waiting for steps"
                )
                # Continue to next stage anyway

        if self._cancelled:
            return

        # Evaluate exit criteria
        can_exit = await workflow.execute_activity(
            evaluate_exit_criteria,
            args=[self._case_id, stage_id],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        if can_exit:
            workflow.logger.info(f"Stage completed: {stage_name}")
        else:
            workflow.logger.warning(
                f"Stage {stage_name} exit criteria not met — continuing anyway"
            )

    # ── Helpers ───────────────────────────────────────────────────

    async def _set_status(self, status: str) -> None:
        """Update case status via activity."""
        self._status = status
        await workflow.execute_activity(
            update_case_status,
            args=[self._case_id, status],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _resolve(self) -> None:
        """Resolve the case."""
        self._status = "resolved"
        await workflow.execute_activity(
            resolve_case,
            self._case_id,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        await workflow.execute_activity(
            send_case_notification,
            args=[self._case_id, "case_resolved", None, None],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    def _result(
        self, status: str, error: str | None = None
    ) -> dict[str, Any]:
        return {
            "case_id": self._case_id,
            "status": status,
            "current_stage_id": self._current_stage_id,
            "completed_steps": list(self._completed_steps),
            "total_stages": len(self._stages),
            "error": error,
        }
