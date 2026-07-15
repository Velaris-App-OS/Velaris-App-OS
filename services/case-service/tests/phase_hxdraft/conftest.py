"""phase_hxdraft-local fixtures.

The draft endpoint shares HxNexus's per-user chat rate limit (20/min). The whole
phase now makes more than 20 draft calls inside one window, so the LATER tests
would 429 purely from suite size — reset the limiter's window between tests.
(The rate-limit behaviour itself is pinned by the P1 endpoint tests.)
"""
from __future__ import annotations

import pytest

from case_service.hxnexus.guard import chat_rate_limiter


@pytest.fixture(autouse=True)
def _reset_chat_rate_limit():
    chat_rate_limiter._windows.clear()
    yield
    chat_rate_limiter._windows.clear()
