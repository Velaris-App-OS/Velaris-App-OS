"""Request tracking middleware for graceful shutdown.

Tracks in-flight requests so the shutdown hook can wait for
them to complete before closing connections.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)


class RequestTrackingMiddleware(BaseHTTPMiddleware):
    """Tracks active request count for graceful shutdown."""

    def __init__(self, app):
        super().__init__(app)
        self._active_requests = 0
        self._lock = asyncio.Lock()

    @property
    def active_requests(self) -> int:
        return self._active_requests

    async def dispatch(self, request: Request, call_next):
        async with self._lock:
            self._active_requests += 1

        start = time.monotonic()
        try:
            response = await call_next(request)
            elapsed = time.monotonic() - start
            response.headers["X-Response-Time"] = f"{elapsed:.3f}s"
            return response
        finally:
            async with self._lock:
                self._active_requests -= 1

    async def wait_for_drain(self, timeout: float = 30.0):
        """Wait for all in-flight requests to complete."""
        start = time.monotonic()
        while self._active_requests > 0:
            if time.monotonic() - start > timeout:
                logger.warning(
                    "Shutdown timeout: %d requests still in-flight",
                    self._active_requests,
                )
                break
            await asyncio.sleep(0.1)
        logger.info("All requests drained")
