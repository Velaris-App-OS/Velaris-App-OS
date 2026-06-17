"""Temporal workflow: CaseLifecycleWorkflow — durable-timer COMPANION.

One execution per case instance. This workflow does NOT drive the case
lifecycle: status, stage, and assignment writes belong exclusively to
the synchronous API path (HxGuard-enforced, audited). The companion's
job is durability for the things that have no other owner — it watches
the case's SLA instances and keeps one SLATimerWorkflow child alive per
instance, so at-risk / breach / escalation events fire on time and
survive restarts.

It learns about changes via best-effort signals from the API routes and
falls back to a periodic rescan, so a missed signal delays a timer's
creation but never loses it. Workflow state holds only IDs (Option A
constraint 3, docs/Future/temporal-decision-record.md).

Start via::

    await client.start_workflow(
        CaseLifecycleWorkflow.run,
        {"case_id": "...", "case_type_id": "..."},
        id=f"helix-case-{case_id}",
        task_queue="helix-case-service",
    )

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from case_service.temporal.activities.sla_timer_activities import (
        list_case_sla_timers,
    )
    from case_service.temporal.workflows.sla_timer_workflow import (
        SLATimerInput,
        SLATimerWorkflow,
    )


_ACTIVITY_TIMEOUT = timedelta(seconds=30)
_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
)

# Rescan even without signals: catches SLA rows created by paths that
# cannot signal (module-level auto-advance callers, rules).
_RESCAN_INTERVAL = timedelta(hours=1)
# After a signal, give the API request's transaction a moment to commit
# before reading.
_SETTLE = timedelta(seconds=5)
_HISTORY_SOFT_LIMIT = 2000

_TERMINAL_STATUSES = ("resolved", "closed", "cancelled")


@workflow.defn(name="helix.case.lifecycle")
class CaseLifecycleWorkflow:
    """Keeps durable SLA timers in sync with a case's SLA instances."""

    def __init__(self) -> None:
        self._case_id: str = ""
        self._known_timers: set[str] = set()
        self._rescan = False
        self._wake_children = False
        self._terminal = False

    # ── Queries ───────────────────────────────────────────────────

    @workflow.query(name="get_case_state")
    def get_case_state(self) -> dict[str, Any]:
        return {
            "case_id": self._case_id,
            "sla_timers": sorted(self._known_timers),
            "terminal": self._terminal,
        }

    # ── Signals (all best-effort nudges; rescan is the safety net) ─

    @workflow.signal(name="step_completed")
    async def step_completed(self, data: dict[str, Any]) -> None:
        """A step finished — the stage may have advanced and started SLAs."""
        self._rescan = True

    @workflow.signal(name="stage_entered")
    async def stage_entered(self, data: dict[str, Any]) -> None:
        """Case entered a stage — stage SLAs may have started."""
        self._rescan = True

    @workflow.signal(name="sla_refresh")
    async def sla_refresh(self, data: dict[str, Any]) -> None:
        """SLA started/paused/resumed via API — rescan and wake timers."""
        self._rescan = True
        self._wake_children = True

    @workflow.signal(name="status_changed")
    async def status_changed(self, data: dict[str, Any]) -> None:
        """Case status changed — SLAs may be paused/resumed/cancelled."""
        self._rescan = True
        self._wake_children = True
        if data.get("status") in _TERMINAL_STATUSES:
            self._terminal = True

    @workflow.signal(name="cancel_case")
    async def cancel_case(self, data: dict[str, Any]) -> None:
        self._terminal = True
        self._wake_children = True

    # ── Main loop ─────────────────────────────────────────────────

    @workflow.run
    async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self._case_id = input_data["case_id"]
        if input_data.get("known_timers"):
            self._known_timers = set(input_data["known_timers"])

        workflow.logger.info(
            "Case SLA companion started: %s (%d known timers)",
            self._case_id, len(self._known_timers),
        )

        # The workflow is started mid-request, before the creating
        # transaction commits — settle before the first scan.
        await workflow.sleep(_SETTLE)

        while True:
            if self._wake_children:
                self._wake_children = False
                await self._wake_all_timers()

            timers = await workflow.execute_activity(
                list_case_sla_timers,
                self._case_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
            )
            for t in timers:
                if t["sla_id"] in self._known_timers:
                    continue
                if t["status"] == "cancelled":
                    continue
                await self._start_timer(t["sla_id"])

            if self._terminal:
                # Final wake so every timer observes its cancelled row
                # and drains; children are ABANDON so they outlive us.
                await self._wake_all_timers()
                return {
                    "case_id": self._case_id,
                    "sla_timers": len(self._known_timers),
                    "status": "terminal",
                }

            try:
                await workflow.wait_condition(
                    lambda: self._rescan or self._terminal,
                    timeout=_RESCAN_INTERVAL,
                )
            except TimeoutError:
                pass
            if self._rescan:
                self._rescan = False
                await workflow.sleep(_SETTLE)

            if workflow.info().get_current_history_length() > _HISTORY_SOFT_LIMIT:
                workflow.continue_as_new({
                    "case_id": self._case_id,
                    "case_type_id": input_data.get("case_type_id", ""),
                    "known_timers": sorted(self._known_timers),
                })

    # ── Helpers ───────────────────────────────────────────────────

    async def _start_timer(self, sla_id: str) -> None:
        try:
            await workflow.start_child_workflow(
                SLATimerWorkflow.run,
                SLATimerInput(sla_id=sla_id, case_id=self._case_id),
                id=f"helix-sla-{sla_id}",
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
        except Exception as e:
            # Already started (e.g. by a pre-continue-as-new run) — the
            # timer exists, which is all we need.
            workflow.logger.info(
                "SLA timer %s not started (%s) — treating as existing",
                sla_id, type(e).__name__,
            )
        self._known_timers.add(sla_id)

    async def _wake_all_timers(self) -> None:
        for sla_id in sorted(self._known_timers):
            try:
                handle: Any = workflow.get_external_workflow_handle(
                    f"helix-sla-{sla_id}"
                )
                await handle.signal("wake")
            except Exception:
                pass  # timer already completed — nothing to wake
