"""Temporal workflow: SLATimerWorkflow — durable clock for ONE SLA instance.

DB-truth design: the workflow stores only the SLA instance id. On every
loop it asks an activity what the database says (status, next due event),
sleeps durably until that moment, then fires an idempotent activity that
re-verifies before writing. Pause/resume/cancel therefore need no state
in the workflow — a "wake" signal just cuts the sleep short so the next
DB read is picked up immediately; without a signal the timer still
converges at the next event or periodic recheck.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from case_service.temporal.activities.sla_timer_activities import (
        fire_sla_event,
        get_sla_timer_state,
    )


@dataclasses.dataclass
class SLATimerInput:
    sla_id: str
    case_id: str = ""


_ACTIVITY_TIMEOUT = timedelta(seconds=30)
# Durability over giving up: retry DB-touching activities indefinitely
# with capped backoff; the timer must survive transient outages.
_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
)

# Re-read DB state at least this often even with no event due and no
# wake signal (catches resumes/cancellations whose signal was lost).
_RECHECK_INTERVAL = timedelta(hours=6)
# Cap individual sleeps so long-deadline timers periodically resync.
_MAX_SLEEP = timedelta(hours=24)
_HISTORY_SOFT_LIMIT = 2000


@workflow.defn(name="helix.sla.timer")
class SLATimerWorkflow:
    """Fires at-risk / breach / escalation-level events for one SLA row."""

    def __init__(self) -> None:
        self._wake = False

    @workflow.signal
    async def wake(self) -> None:
        """SLA row changed (paused/resumed/cancelled) — re-read state now."""
        self._wake = True

    @workflow.run
    async def run(self, params: SLATimerInput) -> dict:
        events_fired = 0

        while True:
            state = await workflow.execute_activity(
                get_sla_timer_state,
                params.sla_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
            )

            if state["terminal"]:
                return {
                    "sla_id": params.sla_id,
                    "final_status": state["status"],
                    "events_fired": events_fired,
                }

            if state["paused"] or state["next_event_at"] is None:
                await self._sleep_or_wake(_RECHECK_INTERVAL)
                continue

            next_at = datetime.fromisoformat(state["next_event_at"])
            delay = next_at - workflow.now()
            if delay > _MAX_SLEEP:
                await self._sleep_or_wake(_MAX_SLEEP)
                continue
            if delay > timedelta(0):
                woke = await self._sleep_or_wake(delay)
                if woke:
                    continue  # state changed under us — re-read before firing

            result = await workflow.execute_activity(
                fire_sla_event,
                params.sla_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
            )
            events_fired += result.get("events_fired", 0)

            if workflow.info().get_current_history_length() > _HISTORY_SOFT_LIMIT:
                workflow.continue_as_new(params)

    async def _sleep_or_wake(self, timeout: timedelta) -> bool:
        """Durable sleep that a wake signal can cut short.

        Returns True if woken by signal, False on timeout.
        """
        try:
            await workflow.wait_condition(
                lambda: self._wake, timeout=timeout
            )
        except TimeoutError:
            pass
        woke = self._wake
        self._wake = False
        return woke
