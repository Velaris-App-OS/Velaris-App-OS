"""worker-service — case-service's own code started in worker mode (R5e step 9).

Runs the background jobs (`case_service/jobs.py`: the scheduler, the email poll,
the sealers, sweeps and watchers) and nothing else: no API routes, only
`/health`, `/ready` and `/metrics` on loopback. It is case-service's code, not a
separate package — the jobs are case core (the scheduler opens cases, the
sealers write the chains), and moving them would mean an internal route per job.

Every singleton job runs under a job lease (`velaris_shared.jobs.leases`), so
this process and case-service (while `VELARIS_CASE_RUN_JOBS` is unset) can both
start the jobs and one of them runs each. Its own process means a heavy sweep
no longer shares the event loop that serves case transitions.

What the jobs need from a process, it starts too: the platform lifespan (the
superadmin gate, the role store, both generation polls), the HxVault data keys,
the feature flags, the Temporal client (a case the scheduler opens still gets
its lifecycle workflow — but not the embedded Temporal worker, which stays in
case-service), and the realtime bridge when the cache is on, so a job's live
update reaches the Studio users connected to case-service.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

# Before anything else is imported: the internal client and the TLS arguments
# are chosen by it.
os.environ.setdefault("VELARIS_SERVICE_NAME", "worker-service")

from fastapi import FastAPI  # noqa: E402

from velaris_shared.app import (  # noqa: E402
    apply_platform_error_handlers,
    apply_platform_middleware,
    mount_metrics,
    platform_lifespan,
)
from velaris_shared.config import get_settings  # noqa: E402
from velaris_shared.observability import configure_logging  # noqa: E402

logger = logging.getLogger("case_service.worker")

SERVICE_NAME = "worker-service"


def _lifespan():
    base = platform_lifespan(SERVICE_NAME)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with base(app):
            from velaris_shared.core.feature_flags import refresh_version_cache
            from velaris_shared.db.session import get_session_factory

            from case_service import hxvault

            try:
                async with get_session_factory()() as session:
                    await hxvault.warm_cache(session)
                    await refresh_version_cache(session)
            except Exception as exc:                            # noqa: BLE001
                logger.error("worker-service: start-up cache fill failed: %s", exc)

            tasks: list[asyncio.Task] = []
            interval = get_settings().vault_cache_resync_seconds
            if interval and interval > 0:
                async def _vault_resync_loop():
                    while True:
                        await asyncio.sleep(interval)
                        try:
                            async with get_session_factory()() as session:
                                await hxvault.resync_cache(session)
                        except Exception as exc:                # noqa: BLE001
                            logger.warning("HxVault resync failed: %s", exc)
                tasks.append(asyncio.create_task(_vault_resync_loop()))

            # The Temporal CLIENT only: a case a job opens starts its lifecycle
            # workflow, which case-service's embedded worker executes.
            try:
                from case_service.temporal.worker import connect_temporal, set_client
                set_client(await connect_temporal())
                logger.info("Temporal client connected")
            except Exception as exc:                            # noqa: BLE001
                logger.warning("Temporal unavailable: %s — cases opened by jobs start "
                               "without their lifecycle workflow", exc)

            bridge = None
            try:
                s = get_settings()
                if getattr(s, "redis_enabled", False):
                    from velaris_shared.redis_client import get_redis

                    from case_service.realtime.manager import get_manager
                    from case_service.realtime.redis_bridge import RedisBridge
                    r = await get_redis()
                    if r is not None:
                        bridge = RedisBridge(get_manager(), r, prefix=s.redis_channel_prefix,
                                             instance_id=f"{SERVICE_NAME}-{os.getpid()}")
                        await bridge.start()
                        get_manager().attach_redis_bridge(bridge)
            except Exception as exc:                            # noqa: BLE001
                logger.warning("Realtime bridge unavailable: %s", exc)

            from case_service.jobs import start_jobs
            jobs = await start_jobs()
            logger.info("worker-service: background jobs started")
            try:
                yield
            finally:
                await jobs.stop()
                for t in tasks:
                    t.cancel()
                if bridge is not None:
                    try:
                        await bridge.stop()
                    except Exception:                           # noqa: BLE001
                        pass

    return lifespan


def create_app() -> FastAPI:
    app = FastAPI(title="Velaris worker-service", version="0.1.0",
                  description="case-service's background jobs.",
                  docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan())
    mount_metrics(app)
    apply_platform_error_handlers(app, "case_service.worker.errors")
    apply_platform_middleware(app)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "healthy", "service": SERVICE_NAME}

    @app.get("/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        from sqlalchemy import text

        from velaris_shared.db.session import get_session_factory
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready", "service": SERVICE_NAME}

    configure_logging()
    return app


app = create_app()
