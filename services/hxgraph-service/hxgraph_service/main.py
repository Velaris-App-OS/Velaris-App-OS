"""hxgraph-service — HxGraph in its own process (roadmap 13d).

Serves `/api/v1/graph`: sync from the database, natural-language query, path
finding, impact analysis, the report, the JSON export and the interactive
visualizer.

It is extracted for the same reason analytics was: the work is read-heavy and
bursty — an embedding pass and a community detection run over the whole graph
should not compete for case-service's transactional connection pool. The graph
tables live in the same database, reached through this service's own pool.

The write side that other modules trigger (`sync_graph`, `query_graph`) stays a
shared library, exactly as `process_mining.event_logger` did at 13c: the
scheduler, HxDBMigrate and the HxMCP tool registry all call it in-process, and
turning those into REST calls would rewrite three callers to move one module.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from velaris_shared.db.commit_route import install_commit_before_response
from velaris_shared.app import (
    apply_platform_error_handlers,
    apply_platform_middleware,
    mount_metrics,
    platform_lifespan,
)
from velaris_shared.config import get_settings
from velaris_shared.observability import configure_logging, configure_telemetry

from hxgraph_service.api.routers.graph import router as graph_router

logger = logging.getLogger("hxgraph_service")

SERVICE_NAME = "hxgraph-service"
API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Velaris hxgraph-service",
        version="0.1.0",
        description="HxGraph — the Velaris native knowledge graph.",
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        # The superadmin boot gate: the middleware only reads the flag this
        # lifespan sets, so the two must always be wired together.
        lifespan=platform_lifespan(SERVICE_NAME),
    )

    # Commit each request's database work BEFORE its response is sent. Must be
    # set before any include_router below: FastAPI captures the route class at
    # registration time. See velaris_shared/db/commit_route.py.
    install_commit_before_response(app)

    app.include_router(graph_router, prefix=API_PREFIX)

    mount_metrics(app)
    apply_platform_error_handlers(app, "hxgraph_service.errors")
    apply_platform_middleware(app)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "healthy", "service": SERVICE_NAME}

    @app.get("/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        from sqlalchemy import text

        from velaris_shared.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready", "service": SERVICE_NAME}

    configure_logging()
    try:
        configure_telemetry(SERVICE_NAME, app=app)
    except Exception:  # noqa: BLE001 - telemetry must never block startup
        logger.warning("telemetry not configured for %s", SERVICE_NAME, exc_info=True)

    return app


app = create_app()
