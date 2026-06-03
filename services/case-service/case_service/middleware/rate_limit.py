"""Simple in-memory rate limiter middleware.

For production, swap with Redis-backed limiter.
Token bucket algorithm per client IP.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    capacity: float
    refill_rate: float  # tokens per second
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)

    def consume(self, now: float | None = None) -> bool:
        now = now or time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    @property
    def retry_after(self) -> float:
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.refill_rate


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limiter using token bucket algorithm.

    Parameters
    ----------
    requests_per_minute : int
        Maximum sustained request rate per client IP.
    burst : int
        Maximum burst size (bucket capacity).
    exclude_paths : list[str]
        Paths to exclude from rate limiting (e.g., /health).
    """

    def __init__(
        self,
        app,
        requests_per_minute: int = 120,
        burst: int = 30,
        exclude_paths: list[str] | None = None,
    ):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.refill_rate = requests_per_minute / 60.0
        self.burst = burst
        self.exclude_paths = set(exclude_paths or ["/health", "/ready"])
        self._buckets: dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(
                capacity=self.burst,
                refill_rate=self.refill_rate,
                tokens=self.burst,
            )
        )
        # Cleanup old buckets periodically
        self._last_cleanup = time.monotonic()

    async def dispatch(self, request: Request, call_next):
        # Skip excluded paths
        if request.url.path in self.exclude_paths:
            return await call_next(request)

        # Get client IP
        client_ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()

        # Bypass rate limiting for localhost (load tests, dev tooling)
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            return await call_next(request)

        bucket = self._buckets[client_ip]

        if not bucket.consume():
            retry_after = max(1, int(bucket.retry_after))
            logger.warning(
                "Rate limit exceeded for %s on %s",
                client_ip, request.url.path,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "retry_after_seconds": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self.requests_per_minute),
                },
            )

        response = await call_next(request)

        # Add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(int(bucket.tokens))

        # Periodic cleanup (every 5 min)
        now = time.monotonic()
        if now - self._last_cleanup > 300:
            self._cleanup(now)
            self._last_cleanup = now

        return response

    def _cleanup(self, now: float):
        """Remove stale buckets (no activity for 10 min)."""
        stale = [
            ip for ip, bucket in self._buckets.items()
            if now - bucket.last_refill > 600
        ]
        for ip in stale:
            del self._buckets[ip]
        if stale:
            logger.debug("Cleaned up %d stale rate limit buckets", len(stale))
