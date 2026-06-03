"""Temporal workflow: SLATimerWorkflow.

Child workflow that tracks a single SLA policy instance.
Uses Temporal timers for at-risk and breach deadlines.
Supports pause/resume via signals.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from case_service.temporal.activities.sla_activities import (
        mark_sla_at_risk,
        mark_sla_breached,
        execute_sla_escalations,
    )


@dataclasses.dataclass
class SLATimerParams:
    case_id: str
    sla_policy_id: str
    target_id: str
    goal_seconds: int
    deadline_seconds: int
    at_risk_threshold: float = 0.8


@dataclasses.dataclass
class SLATimerResult:
    case_id: str
    sla_policy_id: str
    final_status: str


ACTIVITY_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class SLATimerWorkflow:
    """Tracks a single SLA with at-risk and breach timers.

    Signals:
        pause   — stop the clock (e.g. pending external)
        resume  — restart the clock
        cancel  — SLA no longer needed (case resolved/cancelled)
    """

    def __init__(self) -> None:
        self._paused: bool = False
        self._cancelled: bool = False
        self._resume_requested: bool = False

    @workflow.signal
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal
    async def resume(self) -> None:
        if self._paused:
            self._paused = False
            self._resume_requested = True

    @workflow.signal
    async def cancel(self) -> None:
        self._cancelled = True

    @workflow.run
    async def run(self, params: SLATimerParams) -> SLATimerResult:
        # Calculate durations
        at_risk_seconds = int(
            params.goal_seconds * params.at_risk_threshold
        )
        remaining_to_goal = params.goal_seconds - at_risk_seconds
        remaining_to_deadline = (
            params.deadline_seconds - params.goal_seconds
        )

        # ── Phase 1: wait until at-risk threshold ─────────────
        elapsed = 0
        target = at_risk_seconds
        while elapsed < target:
            if self._cancelled:
                return SLATimerResult(
                    case_id=params.case_id,
                    sla_policy_id=params.sla_policy_id,
                    final_status="cancelled",
                )

            if self._paused:
                # Wait for resume or cancel
                await workflow.wait_condition(
                    lambda: self._resume_requested or self._cancelled
                )
                self._resume_requested = False
                continue

            chunk = min(target - elapsed, 60)  # check every minute
            try:
                await workflow.wait_condition(
                    lambda: self._paused or self._cancelled,
                    timeout=timedelta(seconds=chunk),
                )
            except TimeoutError:
                pass
            if not self._paused:
                elapsed += chunk

        # Mark at-risk
        if not self._cancelled:
            await workflow.execute_activity(
                mark_sla_at_risk,
                args=[params.case_id, params.sla_policy_id],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
            )

        # ── Phase 2: wait until goal (remaining after at-risk) ─
        elapsed = 0
        target = remaining_to_goal
        while elapsed < target:
            if self._cancelled:
                return SLATimerResult(
                    case_id=params.case_id,
                    sla_policy_id=params.sla_policy_id,
                    final_status="cancelled",
                )
            if self._paused:
                await workflow.wait_condition(
                    lambda: self._resume_requested or self._cancelled
                )
                self._resume_requested = False
                continue

            chunk = min(target - elapsed, 60)
            try:
                await workflow.wait_condition(
                    lambda: self._paused or self._cancelled,
                    timeout=timedelta(seconds=chunk),
                )
            except TimeoutError:
                pass
            if not self._paused:
                elapsed += chunk

        # ── Phase 3: wait from goal to hard deadline ──────────
        elapsed = 0
        target = remaining_to_deadline
        while elapsed < target:
            if self._cancelled:
                return SLATimerResult(
                    case_id=params.case_id,
                    sla_policy_id=params.sla_policy_id,
                    final_status="cancelled",
                )
            if self._paused:
                await workflow.wait_condition(
                    lambda: self._resume_requested or self._cancelled
                )
                self._resume_requested = False
                continue

            chunk = min(target - elapsed, 60)
            try:
                await workflow.wait_condition(
                    lambda: self._paused or self._cancelled,
                    timeout=timedelta(seconds=chunk),
                )
            except TimeoutError:
                pass
            if not self._paused:
                elapsed += chunk

        # ── Breach ────────────────────────────────────────────
        if not self._cancelled:
            await workflow.execute_activity(
                mark_sla_breached,
                args=[params.case_id, params.sla_policy_id],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
            )
            await workflow.execute_activity(
                execute_sla_escalations,
                args=[params.case_id, params.sla_policy_id],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
            )

        return SLATimerResult(
            case_id=params.case_id,
            sla_policy_id=params.sla_policy_id,
            final_status="breached" if not self._cancelled else "cancelled",
        )
