"""Commit a request's database work BEFORE its response is sent.

`get_session` and its siblings are FastAPI ``yield`` dependencies: their code
after the ``yield`` — the commit — runs in the request's exit stack, which
FastAPI unwinds *after* the response has gone to the client. The API therefore
answers ``201 {"id": ...}`` while the row is still uncommitted, and a caller
that immediately fetches that id can get a 404.

Measured on MySQL: 2 of 8 immediate read-after-write pairs failed with no
delay, 10 of 12 under load, 0 of 8 with a 0.5s pause. PostgreSQL has the same
race with a window small enough that it is almost never observed — the defect
is the platform's, not the database's.

This route class closes the window: the handler runs, every session opened for
that request is committed, and only then is the response returned. A commit
that fails now surfaces as an error to the caller instead of a success reply
over data that was never written.

The ``yield`` dependencies keep their own commit as a harmless safety net: a
session with nothing pending commits to a no-op, and a session opened outside
a request (scripts, workers, background tasks) still behaves exactly as before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)

#: Where the per-request session list lives. Request state is used rather than
#: a ContextVar because dependency solving and the route handler must agree on
#: the same object even when FastAPI runs parts of the request in a threadpool.
_STATE_ATTR = "velaris_db_sessions"


def register_session(request: "Request | None", session: Any) -> None:
    """Record a session so the route can commit it before responding.

    A ``None`` request means the session was opened outside an HTTP request —
    a script, a worker, a background task — and nothing is registered.
    """
    if request is None:
        return
    try:
        sessions = getattr(request.state, _STATE_ATTR, None)
        if sessions is None:
            sessions = []
            setattr(request.state, _STATE_ATTR, sessions)
        sessions.append(session)
    except Exception:  # pragma: no cover - state must never break a request
        log.debug("could not register session for early commit", exc_info=True)


def request_session(request: Any) -> Any:
    """The session this request already holds, or None.

    For lookups made inside a request (the caller's access group, a token
    check). Opening a second session from the same pool while the request's
    own session holds a connection in a transaction deadlocks under load:
    every connection ends up held by a request waiting for another one (found
    on a clean box, 2026-10-19 — case creates stalled for up to two minutes
    behind PgBouncer). Reusing the request's session takes no second connection.
    """
    state = getattr(request, "state", None)
    if state is None:
        return None
    session = getattr(state, "db_session", None)
    if session is not None:
        return session
    sessions = getattr(state, _STATE_ATTR, None) or []
    return sessions[0] if sessions else None


async def _commit_registered(request: Request) -> None:
    for session in list(getattr(request.state, _STATE_ATTR, []) or []):
        # in_transaction() is false for a read-only request, so a GET pays
        # nothing here.
        if session.in_transaction():
            await session.commit()


async def _rollback_registered(request: Request) -> None:
    for session in list(getattr(request.state, _STATE_ATTR, []) or []):
        try:
            if session.in_transaction():
                await session.rollback()
        except Exception:  # pragma: no cover - already unwinding
            log.debug("rollback during request teardown failed", exc_info=True)


class CommitBeforeResponseRoute(APIRoute):
    """APIRoute that commits the request's sessions before returning."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except Exception:
                await _rollback_registered(request)
                raise
            # The handler succeeded. Commit now, while we can still turn a
            # failure into an error response — after this point the status
            # line is the client's truth.
            await _commit_registered(request)
            return response

        return handler


class CommitBeforeResponseMiddleware(BaseHTTPMiddleware):
    """Commit the request's sessions between the handler and the response.

    A middleware rather than a route class: ``include_router`` re-registers each
    sub-router's routes with ``route_class_override=type(route)``, so setting
    ``app.router.route_class`` reaches none of the included routers — every one
    of this platform's ~800 routes arrives that way. The middleware wraps them
    all, whatever class they are.

    ``call_next`` returns once the endpoint has produced its response and before
    that response is written to the client, which is exactly the window needed.
    """

    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            await _rollback_registered(request)
            raise
        # A failed commit must not be reported as a success, so let it raise:
        # the platform error handlers turn it into a 500.
        await _commit_registered(request)
        return response


def install_commit_before_response(app: FastAPI) -> None:
    """Commit each request's database work before its response is sent.

    Safe to call at any point during app construction — unlike a route class,
    this does not depend on being installed before ``include_router``.
    WebSocket traffic is unaffected: BaseHTTPMiddleware only sees HTTP.
    """
    app.add_middleware(CommitBeforeResponseMiddleware)
