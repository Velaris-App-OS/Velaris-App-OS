"""
Temporal Worker
================

Starts a Temporal worker that listens on the "helix-engine" task queue
and executes workflows and activities.

The worker can run:
  1. **Embedded** — inside the FastAPI process (for development).
  2. **Standalone** — as a separate process (for production scaling).

Embedded mode (development)::

    # In main.py lifespan:
    from helix_engine.temporal.worker import start_worker, stop_worker
    worker = await start_worker(client)
    yield
    await stop_worker(worker)

Standalone mode (production)::

    python -m helix_engine.temporal.worker

In production, you typically run multiple worker processes to handle
load.  Each worker independently polls the "helix-engine" task queue.

The worker auto-registers:
  - ``ProcessWorkflow``    (from workflows/)
  - All activities          (from activities/ACTIVITY_LIST)
"""

from __future__ import annotations

import asyncio
import os

import structlog
from temporalio.client import Client
from temporalio.worker import Worker

from helix_engine.temporal.workflows import ProcessWorkflow
from helix_engine.temporal.activities import ACTIVITY_LIST

logger = structlog.get_logger()

DEFAULT_TASK_QUEUE = "helix-engine"


async def start_worker(
    client: Client,
    task_queue: str | None = None,
) -> Worker:
    """
    Create and start a Temporal worker.

    The worker runs in the background, polling for tasks.
    Call ``stop_worker()`` during shutdown to clean up.

    Args:
        client: Connected Temporal client.
        task_queue: Task queue name (default: "helix-engine").

    Returns:
        Running Worker instance.
    """
    queue = task_queue or os.environ.get("TEMPORAL_TASK_QUEUE", DEFAULT_TASK_QUEUE)

    worker = Worker(
        client,
        task_queue=queue,
        workflows=[ProcessWorkflow],
        activities=ACTIVITY_LIST,
    )

    # Start the worker in the background
    asyncio.create_task(worker.run())

    logger.info("temporal_worker_started",
                 task_queue=queue,
                 workflows=["ProcessWorkflow"],
                 activities=[a.fn.__name__ if hasattr(a, 'fn') else str(a) for a in ACTIVITY_LIST])

    return worker


async def stop_worker(worker: Worker) -> None:
    """Gracefully shut down a running worker."""
    logger.info("temporal_worker_stopping")
    worker.shutdown()
    logger.info("temporal_worker_stopped")


# ═══════════════════════════════════════════════════════════════════════
#  Standalone entry point
# ═══════════════════════════════════════════════════════════════════════

async def _main() -> None:
    """Run the worker as a standalone process."""
    from helix_engine.temporal.client import connect

    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    queue = os.environ.get("TEMPORAL_TASK_QUEUE", DEFAULT_TASK_QUEUE)

    logger.info("worker_standalone_starting", host=host, task_queue=queue)

    client = await connect(host=host)

    worker = Worker(
        client,
        task_queue=queue,
        workflows=[ProcessWorkflow],
        activities=ACTIVITY_LIST,
    )

    logger.info("worker_standalone_running", task_queue=queue)
    await worker.run()  # Blocks until shutdown


if __name__ == "__main__":
    asyncio.run(_main())
