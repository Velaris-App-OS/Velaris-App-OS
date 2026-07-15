"""Shared fixtures for the HxMCP phase.

The MCP transport rate-limits per user id, and the test admin token carries a
fixed id — so without a reset the limiter's 30/min bucket is shared across the
whole phase and later tests spuriously 429. Give every test a fresh, generous
limiter.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_mcp_rate_limiter(monkeypatch):
    from case_service.api.routers import hxmcp as transport
    from case_service.hxnexus.guard import _RateLimiter
    monkeypatch.setattr(transport, "_rate_limiter",
                        _RateLimiter(max_calls=10_000, window_seconds=60))


@pytest.fixture(autouse=True)
def _fresh_mcp_ext_rate_limiter(monkeypatch):
    from case_service.api.routers import hxmcp as transport
    from case_service.hxnexus.guard import _RateLimiter
    monkeypatch.setattr(transport, "_ext_rate_limiter",
                        _RateLimiter(max_calls=10_000, window_seconds=60))
