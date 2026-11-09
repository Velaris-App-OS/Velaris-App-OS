"""request_session: lookups inside a request reuse the request's own session.

A second session from the same pool, opened while the request's session holds
a connection in a transaction, deadlocked case creation under load (clean box,
2026-10-19). get_current_user and the agent-token dependency now take the
request's session when it has one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from types import SimpleNamespace

from velaris_shared.db.commit_route import register_session, request_session


def _request():
    return SimpleNamespace(state=SimpleNamespace())


def test_no_session_yet_is_none():
    assert request_session(_request()) is None


def test_no_state_is_none():
    assert request_session(SimpleNamespace()) is None
    assert request_session(None) is None


def test_registered_session_is_reused():
    req, first, second = _request(), object(), object()
    register_session(req, first)
    register_session(req, second)
    assert request_session(req) is first


def test_explicit_db_session_wins():
    req, registered, explicit = _request(), object(), object()
    register_session(req, registered)
    req.state.db_session = explicit
    assert request_session(req) is explicit
