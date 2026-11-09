"""Case-service Temporal worker.

Starts a Temporal worker on the "helix-case-service" task queue that
executes the SLA companion (CaseLifecycleWorkflow), per-instance SLA
timers (SLATimerWorkflow), and their direct-repository activities.

Can run embedded (in the FastAPI process) or standalone.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from case_service.temporal.workflows.case_lifecycle_workflow import (
    CaseLifecycleWorkflow,
)
from case_service.temporal.workflows.sla_timer_workflow import (
    SLATimerWorkflow,
)
from case_service.temporal.activities.sla_timer_activities import (
    fire_sla_event,
    get_sla_timer_state,
    list_case_sla_timers,
)
from case_service.temporal.workflows.case_interpreter_workflow import (
    CaseInterpreterWorkflow,
)
from case_service.temporal.activities.interpreter_activities import (
    case_status,
    enter_stage,
    load_case_plan,
    try_advance,
)

logger = logging.getLogger(__name__)

DEFAULT_TASK_QUEUE = "helix-case-service"

# The interpreter (#21) is registered unconditionally so the worker can serve
# it, but it exits immediately for any case type that has not declared
# `orchestration: temporal` — registration is not activation.
CASE_WORKFLOWS = [CaseLifecycleWorkflow, SLATimerWorkflow, CaseInterpreterWorkflow]
CASE_ACTIVITIES = [
    list_case_sla_timers,
    get_sla_timer_state,
    fire_sla_event,
    load_case_plan,
    enter_stage,
    try_advance,
    case_status,
]


#: The process-wide client, set by `main.py` at startup beside
#: `app.state.temporal_client`. Background loops (the scheduler, for one) have no
#: request and therefore no `app.state`, and a case opened by a cron line needs
#: its lifecycle workflow exactly as much as one opened by a person does.
_client: "Client | None" = None


def set_client(client) -> None:
    global _client
    _client = client


def get_client():
    """The Temporal client, or None when Temporal is unavailable.

    None is a normal answer, not an error: this deployment may not run Temporal,
    and every caller treats a missing client as "no durable timers", never as a
    failed operation.
    """
    return _client


async def connect_temporal(
    host: str | None = None,
    namespace: str | None = None,
) -> Client:
    """Connect to the Temporal server (address and TLS: velaris_shared.temporal_connect)."""
    from velaris_shared import temporal_connect as tc

    host = host or tc.temporal_target()
    namespace = namespace or tc.temporal_namespace()
    logger.info("Connecting to Temporal: %s (ns: %s)", host, namespace)
    client = await tc.connect(host, namespace)
    logger.info("Connected to Temporal: %s", host)
    return client


async def start_worker(
    client: Client,
    task_queue: str | None = None,
) -> Worker:
    """Create and start an embedded Temporal worker (background task)."""
    queue = task_queue or os.environ.get(
        "VELARIS_CASE_TASK_QUEUE", DEFAULT_TASK_QUEUE
    )
    worker = Worker(
        client,
        task_queue=queue,
        workflows=CASE_WORKFLOWS,
        activities=CASE_ACTIVITIES,
    )
    # Kept so stop_worker can wait for it; an unreferenced task is also one
    # the event loop may garbage-collect mid-run.
    _run_tasks[id(worker)] = asyncio.create_task(worker.run())
    logger.info(
        "Case-service Temporal worker started: queue=%s, "
        "workflows=[CaseLifecycleWorkflow, SLATimerWorkflow], activities=%d",
        queue,
        len(CASE_ACTIVITIES),
    )
    return worker


#: worker → its run() task, so shutdown can wait for it to finish.
_run_tasks: dict[int, asyncio.Task] = {}

#: Seconds stop_worker waits for a clean shutdown. Below the unit's
#: TimeoutStopSec (20 s), so a stuck poll is cancelled here, not SIGKILLed.
STOP_TIMEOUT_SECONDS = 10.0


async def stop_worker(worker: Worker) -> None:
    """Shut the worker down and wait for it, inside the app's lifespan.

    `worker.shutdown()` is a coroutine. It was called without `await`, so it
    never ran: the lifespan ended with the worker still polling, and the
    interpreter's exit then cancelled it mid-poll. That sometimes hung until
    systemd's stop timeout SIGKILLed the process (2 of 17 stops, week of
    2026-10-12). Bounded here: a shutdown that does not finish in time has its
    run task cancelled, so the process still exits on its own.
    """
    logger.info("Stopping case-service Temporal worker")
    task = _run_tasks.pop(id(worker), None)
    try:
        await asyncio.wait_for(worker.shutdown(), timeout=STOP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("Temporal worker did not stop within %.0fs — cancelling it",
                       STOP_TIMEOUT_SECONDS)
    except Exception as exc:                                     # noqa: BLE001
        logger.warning("Temporal worker shutdown failed: %s", exc)
    if task is not None and not task.done():
        # A clean shutdown lets run() return on its own; give it a moment first.
        await asyncio.wait({task}, timeout=2.0)
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
    logger.info("Case-service Temporal worker stopped")


# ── Standalone entry point ────────────────────────────────────────

async def _main() -> None:
    """Run the worker as a standalone process."""
    from velaris_shared.temporal_connect import temporal_target

    host = temporal_target()
    queue = os.environ.get("VELARIS_CASE_TASK_QUEUE", DEFAULT_TASK_QUEUE)

    logging.basicConfig(level=logging.INFO)
    logger.info("Starting standalone case worker: %s → %s", host, queue)

    client = await connect_temporal(host=host)
    worker = Worker(
        client,
        task_queue=queue,
        workflows=CASE_WORKFLOWS,
        activities=CASE_ACTIVITIES,
    )
    logger.info("Case worker running on queue: %s", queue)
    await worker.run()  # Blocks


if __name__ == "__main__":
    asyncio.run(_main())
