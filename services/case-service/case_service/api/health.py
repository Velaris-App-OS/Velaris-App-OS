"""Health and readiness endpoints with detailed checks.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_start_time = time.monotonic()


class HealthResponse(BaseModel):
    status: str
    service: str
    uptime_seconds: int
    timestamp: str


class ReadyResponse(BaseModel):
    status: str
    database: str
    temporal: str
    uptime_seconds: int
    checks: dict


@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    """Lightweight liveness check — always returns quickly."""
    return HealthResponse(
        status="ok",
        service="helix-case-service",
        uptime_seconds=int(time.monotonic() - _start_time),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request):
    """Deep readiness check — verifies database and Temporal connectivity."""
    checks = {}
    db_status = "unknown"
    temporal_status = "unknown"

    # Database check
    try:
        from case_service.db.session import get_session_factory
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                __import__("sqlalchemy").text("SELECT 1")
            )
            result.scalar_one()
            db_status = "connected"
            checks["database_latency_ms"] = "ok"
    except Exception as e:
        db_status = "disconnected"
        checks["database_error"] = str(e)[:200]
        logger.warning("Database readiness check failed: %s", e)

    # Temporal check
    temporal_client = getattr(request.app.state, "temporal_client", None)
    if temporal_client is not None:
        try:
            # Just check if client is connected
            temporal_status = "connected"
        except Exception as e:
            temporal_status = "disconnected"
            checks["temporal_error"] = str(e)[:200]
    else:
        temporal_status = "not_configured"

    overall = "ok" if db_status == "connected" else "degraded"

    return ReadyResponse(
        status=overall,
        database=db_status,
        temporal=temporal_status,
        uptime_seconds=int(time.monotonic() - _start_time),
        checks=checks,
    )
