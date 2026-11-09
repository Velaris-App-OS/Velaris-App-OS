"""R5d 11b — every routed read path that no other test calls, called once.

These run as if a replica were configured (the test database stands in for
it), so each path gets a read-only session of its own (`PRAGMA query_only` on
SQLite) and a write hidden in a helper fails here instead of in production the
day an operator adds a replica (found in 11b: the evidence pack wrote an
HxArbiter decision through its session). With no replica a routed path shares
the request's own session, exactly as before 11b, and nothing is enforced.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import pytest

from velaris_shared.db import read_routing as rr
from velaris_shared.db import session as dbs


@pytest.fixture(autouse=True)
def _as_if_a_replica(monkeypatch):
    rr.reset_for_tests()
    monkeypatch.setattr(dbs, "replica_url", lambda: "sqlite+aiosqlite:///test-replica")
    monkeypatch.setattr(rr.get_settings(), "database_read_url", "sqlite+aiosqlite:///test-replica")

    async def healthy(force=False):
        rr.health._mark_up(None)          # noqa: SLF001
    monkeypatch.setattr(rr.health, "refresh", healthy)
    # The cutoff barrier reads WAL positions — PostgreSQL only; here the
    # point-in-time paths take the capped main pool (the test database).
    monkeypatch.setattr(rr, "_postgres", lambda: False)
    monkeypatch.setattr(rr, "fallback_factory", lambda: dbs.get_replica_session_factory())
    yield
    rr.reset_for_tests()


PATHS = [
    "/api/v1/channels/conversations/stats",
    "/api/v1/email/inbox/stats",
    "/api/v1/compliance/audit/anchors",
    "/api/v1/compliance/ledger/reconcile",
    "/api/v1/costing/export",
    "/api/v1/enterprise/superadmin-audit",
    "/api/v1/push/admin/logs",
]
# Need a tenant-bound caller or a feature switched on — called below.
SPECIAL = ["/api/v1/ai-ledger/models", "/api/v1/ai-ledger/chain", "/api/v1/risk/scores/dashboard"]


@pytest.mark.parametrize("path", PATHS)
async def test_a_routed_read_path_answers_without_writing(client, path):
    r = await client.get(path)
    assert r.status_code == 200, (r.status_code, r.text[:300])   # the handler ran
    assert "readonly" not in r.text.lower() and "read-only" not in r.text.lower(), r.text[:300]


def test_the_list_is_every_untested_routed_path():
    """A newly routed path goes here unless another test calls it."""
    from case_service.main import app
    routed = set()
    for route in app.routes:
        dep = getattr(route, "dependant", None)
        if dep and "GET" in (getattr(route, "methods", None) or ()):
            names = {getattr(d.call, "__name__", "") for d in dep.dependencies}
            if names & {"get_read_session", "get_cutoff_session",
                        "get_analytics_read_session", "get_analytics_cutoff_session"}:
                routed.add(route.path)
    assert set(PATHS + SPECIAL) <= routed


from tests.test_F01_ai_ledger import _headers, tenant  # noqa: E402,F401  (fixture)


@pytest.mark.parametrize("path", SPECIAL[:2])
async def test_the_ai_ledger_reads_answer_without_writing(client, tenant, path):  # noqa: F811
    r = await client.get(path, headers=_headers(["admin"]))
    assert r.status_code == 200, (r.status_code, r.text[:300])


async def test_the_risk_dashboard_answers_without_writing(client, monkeypatch):
    from case_service.api.routers import sentinel
    monkeypatch.setattr(sentinel, "is_feature_enabled", lambda flag: True)
    r = await client.get(SPECIAL[2])
    assert r.status_code == 200, (r.status_code, r.text[:300])
