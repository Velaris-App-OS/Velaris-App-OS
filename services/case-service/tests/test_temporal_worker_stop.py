"""Stopping the embedded Temporal worker (case_service.temporal.worker).

What was wrong: `stop_worker` called `worker.shutdown()` — a coroutine — without
`await`, so it never ran. The lifespan ended with the worker still polling and
the interpreter's exit cancelled it mid-poll, which sometimes hung until
systemd's 20 s stop timeout SIGKILLed case-service.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import time

import pytest

from case_service.temporal import worker as w


class _FakeWorker:
    def __init__(self, shutdown_delay: float):
        self.delay = shutdown_delay
        self.stop = asyncio.Event()
        self.shutdown_awaited = False

    async def run(self):
        await self.stop.wait()

    async def shutdown(self):
        await asyncio.sleep(self.delay)
        self.shutdown_awaited = True
        self.stop.set()


def _track(fake: _FakeWorker) -> asyncio.Task:
    task = asyncio.create_task(fake.run())
    w._run_tasks[id(fake)] = task
    return task


@pytest.mark.asyncio
async def test_shutdown_is_awaited_and_the_run_task_finishes():
    fake = _FakeWorker(shutdown_delay=0.05)
    task = _track(fake)
    await w.stop_worker(fake)
    assert fake.shutdown_awaited
    assert task.done() and not task.cancelled()
    assert id(fake) not in w._run_tasks


@pytest.mark.asyncio
async def test_a_stuck_shutdown_is_cancelled_within_the_bound(monkeypatch):
    monkeypatch.setattr(w, "STOP_TIMEOUT_SECONDS", 0.2)
    fake = _FakeWorker(shutdown_delay=60)
    task = _track(fake)
    t = time.monotonic()
    await w.stop_worker(fake)
    assert time.monotonic() - t < 3
    assert task.done()


def test_the_bound_is_below_the_unit_stop_timeout():
    from pathlib import Path
    unit = Path(__file__).resolve().parents[3] / "deploy/systemd"
    texts = [p.read_text() for p in unit.glob("velaris-case-service*.service")]
    stops = [int(line.split("=", 1)[1]) for t in texts for line in t.splitlines()
             if line.startswith("TimeoutStopSec=")]
    assert stops, "case-service unit has no TimeoutStopSec"
    # The worker bound + the 2 s cancel wait must leave the rest of shutdown time.
    assert w.STOP_TIMEOUT_SECONDS + 2 < min(stops)
