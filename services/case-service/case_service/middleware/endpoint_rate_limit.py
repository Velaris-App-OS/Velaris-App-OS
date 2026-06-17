"""Reusable per-endpoint sliding-window rate limiter (D4).

Generalises the Group A /auth/refresh limiter into a dependency factory any
sensitive endpoint can use:

    _login_rl = rate_limit(max_calls=10, window_seconds=60, name="login")

    @router.post("/login", dependencies=[Depends(_login_rl)])
    async def login(...): ...

Per-IP, per-process, in-memory. Memory is bounded: stale IPs are purged when
the table grows past _PURGE_THRESHOLD. Multi-worker deployments multiply the
effective limit by worker count — acceptable for brute-force protection where
the goal is reducing attempts by orders of magnitude, not exact counting.

Skipped under pytest so test suites can hammer endpoints freely.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_PURGE_THRESHOLD = 10_000  # max tracked IPs before stale entries are purged


class SlidingWindowLimiter:
    def __init__(self, max_calls: int, window_seconds: float, name: str) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self.name = name
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        """Record a call for `key`. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()

        window = self._hits.get(key)
        if window is None:
            if len(self._hits) >= _PURGE_THRESHOLD:
                self._purge(now)
            self._hits[key] = deque([now])
            return True, 0

        while window and now - window[0] > self.window:
            window.popleft()

        if len(window) >= self.max_calls:
            retry_after = max(1, int(self.window - (now - window[0])) + 1)
            return False, retry_after

        window.append(now)
        return True, 0

    def _purge(self, now: float) -> None:
        stale = [
            key for key, window in self._hits.items()
            if not window or now - window[-1] > self.window
        ]
        for key in stale:
            del self._hits[key]
        logger.debug("rate_limit[%s]: purged %d stale entries", self.name, len(stale))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(max_calls: int, window_seconds: float, name: str):
    """Build a FastAPI dependency enforcing max_calls per window per client IP."""
    limiter = SlidingWindowLimiter(max_calls, window_seconds, name)

    async def _dependency(request: Request) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            return
        ip = _client_ip(request)
        allowed, retry_after = limiter.allow(ip)
        if not allowed:
            logger.warning("rate_limit[%s]: blocked %s", name, ip)
            raise HTTPException(
                status_code=429,
                detail=f"Too many {name} attempts. Please wait before retrying.",
                headers={"Retry-After": str(retry_after)},
            )

    return _dependency
