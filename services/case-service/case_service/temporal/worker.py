"""Case-service Temporal worker.

Starts a Temporal worker on the "helix-case-service" task queue
that executes CaseLifecycleWorkflow and all case-related activities.

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
from case_service.temporal.activities.stage_activities import (
    create_stage_assignments,
    evaluate_exit_criteria,
    load_case_type_definition,
    resolve_case,
    update_case_stage,
    update_case_status,
)
from case_service.temporal.activities.sla_activities import (
    cancel_case_slas,
    check_sla_status,
    start_sla_tracking,
    start_sla_v2_tracking,
)
from case_service.temporal.activities.notification_activities import (
    send_case_notification,
)

logger = logging.getLogger(__name__)

DEFAULT_TASK_QUEUE = "helix-case-service"

CASE_ACTIVITIES = [
    load_case_type_definition,
    update_case_status,
    update_case_stage,
    create_stage_assignments,
    evaluate_exit_criteria,
    resolve_case,
    start_sla_tracking,
    start_sla_v2_tracking,
    check_sla_status,
    cancel_case_slas,
    send_case_notification,
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
        workflows=[CaseLifecycleWorkflow],
        activities=CASE_ACTIVITIES,
    )
    asyncio.create_task(worker.run())
    logger.info(
        "Case-service Temporal worker started: queue=%s, "
        "workflows=[CaseLifecycleWorkflow], activities=%d",
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
        workflows=[CaseLifecycleWorkflow],
        activities=CASE_ACTIVITIES,
    )
    logger.info("Case worker running on queue: %s", queue)
    await worker.run()  # Blocks


if __name__ == "__main__":
    asyncio.run(_main())
