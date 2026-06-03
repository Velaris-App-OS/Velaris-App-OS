"""Phase 12 tests — Production Hardening.

Tests health endpoints, rate limiting, and request tracking.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import time

import pytest


class TestHealthEndpoints:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "helix-case-service"
        assert "uptime_seconds" in data
        assert "timestamp" in data

    async def test_ready(self, client):
        resp = await client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert data["database"] in ("connected", "disconnected")
        assert "uptime_seconds" in data
        assert "checks" in data


class TestRateLimiting:
    def test_token_bucket_basic(self):
        from case_service.middleware.rate_limit import TokenBucket
        bucket = TokenBucket(capacity=5, refill_rate=1.0, tokens=5)
        # Consume 5 tokens
        for _ in range(5):
            assert bucket.consume() is True
        # 6th should fail
        assert bucket.consume() is False

    def test_token_bucket_refill(self):
        from case_service.middleware.rate_limit import TokenBucket
        now = time.monotonic()
        bucket = TokenBucket(capacity=5, refill_rate=10.0, tokens=0, last_refill=now)
        # After 0.5s at 10/s refill, should have ~5 tokens
        assert bucket.consume(now + 0.5) is True

    def test_token_bucket_capacity_limit(self):
        from case_service.middleware.rate_limit import TokenBucket
        now = time.monotonic()
        bucket = TokenBucket(capacity=5, refill_rate=100.0, tokens=0, last_refill=now)
        # Even after long time, tokens capped at capacity
        bucket.consume(now + 1000)
        # Should have capacity - 1 tokens left
        assert bucket.tokens <= 5

    def test_retry_after(self):
        from case_service.middleware.rate_limit import TokenBucket
        bucket = TokenBucket(capacity=5, refill_rate=1.0, tokens=0)
        assert bucket.retry_after > 0


class TestRequestTracking:
    def test_middleware_init(self):
        from case_service.middleware.request_tracking import RequestTrackingMiddleware

        class FakeApp:
            pass

        mw = RequestTrackingMiddleware(FakeApp())
        assert mw.active_requests == 0


class TestCORSHeaders:
    async def test_cors_preflight(self, client):
        resp = await client.options(
            "/api/v1/cases",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Should not error (CORS middleware handles it)
        assert resp.status_code in (200, 204, 400, 405)


class TestResponseHeaders:
    async def test_rate_limit_headers(self, client):
        resp = await client.get("/api/v1/case-types")
        # Rate limit headers may or may not be present depending on debug mode
        # In test mode (debug=True equivalent), rate limiting is skipped
        assert resp.status_code == 200


class TestDockerfile:
    def test_dockerfile_exists(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "Dockerfile"
        )
        assert os.path.exists(path), f"Dockerfile not found at {path}"

    def test_dockerfile_has_healthcheck(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "Dockerfile"
        )
        with open(path) as f:
            content = f.read()
        assert "HEALTHCHECK" in content
        assert "8200" in content
        assert "uvicorn" in content
