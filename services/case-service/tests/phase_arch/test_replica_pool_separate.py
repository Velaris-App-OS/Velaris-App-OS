"""Without a replica, the read pool is still a pool of its own (R6 soak, 2026-11-02).

An analytics request holds an analytics session (its sign-in check runs there)
AND a read session. When the read engine WAS the analytics engine, each request
took two connections from one pool of 5; five requests each held one and waited
for a second, and every analytics page answered 503 in 31-second waves until the
pool timeout broke the wait. This pins the two pools apart.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from velaris_shared.db import session as dbs


def _fresh(monkeypatch, replica_url: str):
    made = []

    def fake_make(pool_size, max_overflow, url=None, pool_timeout=None):
        made.append(url)
        return object()

    monkeypatch.setattr(dbs, "_make_engine", fake_make)
    monkeypatch.setattr(dbs, "_analytics_engine", None)
    monkeypatch.setattr(dbs, "_replica_engine", None)
    monkeypatch.setattr(dbs, "replica_url", lambda: replica_url)
    return made


def test_no_replica_read_engine_is_not_the_analytics_engine(monkeypatch):
    made = _fresh(monkeypatch, "")
    assert dbs.get_replica_engine() is not dbs.get_analytics_engine()
    assert made == [None, None]          # both on the main database


def test_with_a_replica_the_read_engine_points_at_it(monkeypatch):
    made = _fresh(monkeypatch, "postgresql+asyncpg://replica/x")
    assert dbs.get_replica_engine() is not dbs.get_analytics_engine()
    assert made[0] == "postgresql+asyncpg://replica/x"


def test_the_read_engine_is_built_once(monkeypatch):
    _fresh(monkeypatch, "")
    assert dbs.get_replica_engine() is dbs.get_replica_engine()
