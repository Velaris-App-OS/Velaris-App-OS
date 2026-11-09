"""R6 failure test 2026-11-03: the database died mid-request, the cleanup
rollback failed with a raw driver error, and that error — not the lost
connection — reached the error handlers (500 instead of 503).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import pytest


class _Sess:
    def __init__(self):
        self.info = {}

    async def rollback(self):
        raise RuntimeError("InternalClientError: connection closed")

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_rollback_failure_is_swallowed():
    from velaris_shared.db.session import _rollback_quietly
    await _rollback_quietly(_Sess())  # must not raise


@pytest.mark.asyncio
async def test_release_survives_failed_rollback():
    from velaris_shared.db.read_routing import _release
    await _release(_Sess())  # must not raise


@pytest.mark.asyncio
async def test_dependency_propagates_the_original_error(monkeypatch):
    from velaris_shared.db import session as dbs

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _Sess()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dbs, "get_analytics_session_factory", lambda: _Factory())
    gen = dbs.get_analytics_session(None)
    await gen.__anext__()
    with pytest.raises(ValueError, match="original"):
        await gen.athrow(ValueError("original"))
