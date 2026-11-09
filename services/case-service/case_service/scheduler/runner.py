"""The scheduler loop — claims due schedules and runs their action.

Same shape as `OutboxRelay`, `RetentionRunner`, `EmailPollLoop` and the bot
runner, started from `main.py` beside them, so there is one answer to "where do
background loops live in this service".

**On by default**, unlike the bot runner: this loop does not start containers, it
writes rows and sends mail. A deployment that wants it off sets
`VELARIS_CASE_SCHEDULER_ENABLED=false`. What it fires is still bounded by the
approval gate — with nothing approved, an enabled loop does nothing at all.

The cycle:

    claim every due schedule   (conditional UPDATE on `next_run_at` — two
                                workers cannot both win one firing)
    a missed window is recorded as `skipped`, never replayed
    run the action, one session per firing
    record the run, ok or error, including the ones that did nothing

A failing action does **not** stop the timetable: the next occurrence was
computed at claim time, so a schedule that errors every night keeps trying and
keeps recording, rather than quietly parking itself. That is the opposite of the
bot job ladder on purpose — a job is one piece of work that can be abandoned; a
schedule is a standing instruction.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from case_service.scheduler import service

log = logging.getLogger(__name__)

#: A minute is the resolution of a cron expression, so a shorter tick buys
#: nothing; a longer one delays every schedule by up to its length.
TICK_SECONDS = 20.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SchedulerLoop:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        from velaris_shared.config import get_settings

        if not getattr(get_settings(), "scheduler_enabled", True):
            log.info("scheduler disabled (VELARIS_CASE_SCHEDULER_ENABLED)")
            return
        if self._task is not None:
            return
        # A stop() leaves the event set; a job lease taken back later (R5e
        # step 9) starts the loop again, which must not exit at once.
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        log.info("scheduler started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad schedule must not stop every other schedule.
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: datetime | None = None) -> int:
        """One pass. Returns how many schedules fired (skips included)."""
        from velaris_shared.db.session import get_session_factory

        now = now or _utcnow()
        async with get_session_factory()() as session:
            claimed = await service.claim_due(session, now=now)
            await session.commit()
            ids = [(row.id, missed) for row, missed in claimed]

        for schedule_id, missed in ids:
            await self._fire(schedule_id, missed)
        return len(ids)

    async def _fire(self, schedule_id, missed: bool) -> None:
        from velaris_shared.db.models import ScheduleModel
        from velaris_shared.db.session import get_session_factory
        from case_service.scheduler import actions

        async with get_session_factory()() as session:
            row = await session.get(ScheduleModel, schedule_id)
            if row is None:
                return

            if missed:
                # Recorded, not run. Silence and "it deliberately did not fire"
                # are different answers, and only one of them is debuggable.
                await service.record_run(
                    session, row, status="skipped",
                    detail={"reason": "missed window",
                            "note": "the platform was not running when this was "
                                    "due; the next occurrence stands"})
                await session.commit()
                log.warning("schedule %s missed its window", row.name)
                return

            # An archived (offboarded) tenant's automation stops with its
            # people (R7) — said on the run, never silently.
            from velaris_shared.tenancy.identity import tenant_closed
            if await tenant_closed(session, getattr(row, "tenant_id", None)):
                await service.record_run(session, row, status="skipped",
                                         detail={"reason": "tenant archived"})
                await session.commit()
                return

            handler = actions.get(row.action_kind)
            started = _utcnow()
            if handler is None:
                await service.record_run(
                    session, row, status="error",
                    detail={"error": f"unknown action '{row.action_kind}'"})
                await session.commit()
                return

            from velaris_shared.config import get_settings

            budget = max(5, int(getattr(
                get_settings(), "scheduler_action_timeout_seconds", 300)))
            try:
                # Bounded. Actions run one after another, so an action that never
                # returns — a connector against a supplier that holds the
                # connection open — would stall every OTHER schedule for as long
                # as it hangs, and nothing would say why.
                detail = await asyncio.wait_for(
                    handler.run(session, row, dict(row.action_config or {})),
                    timeout=budget)
                status = "ok"
            except asyncio.TimeoutError:
                log.error("schedule %s exceeded %ss and was abandoned",
                          row.name, budget)
                # The session was cancelled mid-statement, so it has to be rolled
                # back and the row re-read before anything can be recorded.
                await session.rollback()
                row = await session.get(ScheduleModel, schedule_id)
                detail = {"error": f"The action did not finish within {budget}s "
                                   f"and was abandoned. The next firing stands."}
                status = "error"
            except Exception as exc:                       # noqa: BLE001
                log.exception("schedule %s failed", row.name)
                await session.rollback()
                row = await session.get(ScheduleModel, schedule_id)
                detail, status = {"error": str(exc)[:2000]}, "error"

            ms = int((_utcnow() - started).total_seconds() * 1000)
            await service.record_run(
                session, row, status=status, detail=detail or {}, duration_ms=ms)
            await session.commit()


#: The process-wide instance, started from `main.py` beside the other loops.
loop = SchedulerLoop()
