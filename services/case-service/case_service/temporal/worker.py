"""Case-service Temporal worker.

Starts a Temporal worker on the "helix-case-service" task queue that
executes the SLA companion (CaseLifecycleWorkflow), per-instance SLA
timers (SLATimerWorkflow), and their direct-repository activities.

Can run embedded (in the FastAPI process) or standalone.

Copyright (c) 2024-2025 HELIX Contributors
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

logger = logging.getLogger(__name__)

DEFAULT_TASK_QUEUE = "helix-case-service"

CASE_WORKFLOWS = [CaseLifecycleWorkflow, SLATimerWorkflow]
CASE_ACTIVITIES = [
    list_case_sla_timers,
    get_sla_timer_state,
    fire_sla_event,
]


async def connect_temporal(
    host: str | None = None,
    namespace: str | None = None,
) -> Client:
    """Connect to the Temporal server."""
    host = host or os.environ.get("TEMPORAL_HOST", "localhost:7233")
    namespace = namespace or os.environ.get("TEMPORAL_NAMESPACE", "default")
    logger.info("Connecting to Temporal: %s (ns: %s)", host, namespace)
    client = await Client.connect(host, namespace=namespace)
    logger.info("Connected to Temporal: %s", host)
    return client


async def start_worker(
    client: Client,
    task_queue: str | None = None,
) -> Worker:
    """Create and start an embedded Temporal worker (background task)."""
    queue = task_queue or os.environ.get(
        "HELIX_CASE_TASK_QUEUE", DEFAULT_TASK_QUEUE
    )
    worker = Worker(
        client,
        task_queue=queue,
        workflows=CASE_WORKFLOWS,
        activities=CASE_ACTIVITIES,
    )
    asyncio.create_task(worker.run())
    logger.info(
        "Case-service Temporal worker started: queue=%s, "
        "workflows=[CaseLifecycleWorkflow, SLATimerWorkflow], activities=%d",
        queue,
        len(CASE_ACTIVITIES),
    )
    return worker


async def stop_worker(worker: Worker) -> None:
    """Gracefully shut down the worker."""
    logger.info("Stopping case-service Temporal worker")
    worker.shutdown()
    logger.info("Case-service Temporal worker stopped")


# ── Standalone entry point ────────────────────────────────────────

async def _main() -> None:
    """Run the worker as a standalone process."""
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    queue = os.environ.get("HELIX_CASE_TASK_QUEUE", DEFAULT_TASK_QUEUE)

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
