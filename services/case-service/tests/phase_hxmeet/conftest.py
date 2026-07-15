"""phase_hxmeet-local fixtures.

The guest-token/preview rate limiter is a module-global sliding window
(10/min per IP). Every test request shares one ASGI test "IP", and the whole
phase now runs in well under the 60s window — without a reset, unrelated
tests start eating 429s depending on shuffle order. Prod behaviour is
untouched; this only clears the window between tests.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_guest_rate_limiter():
    from case_service.api.routers import meet
    meet._guest_rate._windows.clear()
    yield
    meet._guest_rate._windows.clear()
