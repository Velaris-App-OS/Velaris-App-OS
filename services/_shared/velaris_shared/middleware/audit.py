"""AuditMiddleware — system-wide action logging for every mutating request.

Intercepts every POST/PUT/PATCH/DELETE request and writes one entry to
both trace_events (feeds HxStream live WebSocket) and security_events
(feeds Enterprise > Security Events panel).

This single middleware covers the ~50 routers that don't call emit_trace
manually, giving admins full visibility into every action in the system.
Login/logout are handled explicitly in auth_real.py rather than here
(the middleware has no actor until after login succeeds).

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Methods that mutate state — read-only methods are skipped
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Paths that should never be audit-logged (health, auth introspection, WS)
_SKIP_PREFIXES = (
    "/api/v1/auth/",          # login handled explicitly
    "/api/v1/hxstream/ws",    # WebSocket — can't read body, not a mutation
    "/api/v1/hxstream/event", # frontend fires this for its own UI events
    "/health",
    "/metrics",
    "/docs",
    "/openapi",
    "/redoc",
)


def _extract_user_id(request: Request) -> str | None:
    """Best-effort actor extraction from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    if not token:
        return None
    try:
        from velaris_shared.auth.jwt_handler import decode_jwt_token
        from velaris_shared.config import get_settings
        # public_key or this verifies HS256 only. This one failed *quietly*:
        # the caller swallows the exception and returns None, so on an RS256
        # deployment every audit record lost its actor and the superadmin →
        # __system__ masking below never ran.
        _s = get_settings()
        claims = decode_jwt_token(token, secret=_s.auth_secret,
                                  public_key=_s.auth_rsa_public_key or "")
        user_id = str(claims.get("sub") or claims.get("preferred_username") or "")
        # Superadmin actions are recorded as __system__ to keep identity hidden
        roles = claims.get("realm_access", {}).get("roles", []) or claims.get("roles", [])
        if "superadmin" in roles:
            return "__system__"
        return user_id
    except Exception:
        return None


def _path_label(request: Request) -> str:
    """Return the route template path (e.g. /cases/{case_id}) if available."""
    route = request.scope.get("route")
    if route and hasattr(route, "path"):
        return route.path
    return request.url.path


class AuditMiddleware(BaseHTTPMiddleware):
    """Writes one audit entry per mutating API call.

    Runs AFTER the response is produced so it never adds latency to the
    happy path. Fire-and-forget DB writes with their own session; never
    raises into the request chain.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Fast path — skip non-mutating and excluded paths
        if request.method not in _MUTATING:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # Extract actor before calling next (headers still available after)
        actor_user_id = _extract_user_id(request)
        client_ip = request.client.host if request.client else None

        start = time.perf_counter()
        response = await call_next(request)
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Determine outcome from HTTP status
        status = response.status_code
        if status < 300:
            outcome = "success"
        elif status in (401, 403):
            outcome = "denied"
        elif status >= 400:
            outcome = "error"
        else:
            outcome = "success"

        # Severity escalation for destructive or denied actions
        if request.method == "DELETE":
            severity = "warning"
        elif outcome == "denied":
            severity = "warning"
        elif outcome == "error":
            severity = "warning"
        else:
            severity = "info"

        route_path = _path_label(request)
        event_type = f"api.{request.method.lower()}"
        resource_type = route_path.strip("/").split("/")[2] if route_path.count("/") >= 3 else "api"

        payload = {
            "method":       request.method,
            "path":         route_path,
            "status":       status,
            "latency_ms":   latency_ms,
            "query":        _safe_query(str(request.url.query)) if request.url.query else None,
        }

        # A refused call that repeats (same address, actor, route, status) inside
        # a minute is counted, not written again (S2, R6 soak 2026-11-02:
        # 60,724 rows in 1.5 h of 401s, no real work). The first is written
        # as always; the repeats become one summary row when the minute ends.
        if outcome == "denied" and _fold_repeat(
            (client_ip, actor_user_id, request.method, route_path, status),
            event_type=event_type, severity=severity, actor_user_id=actor_user_id,
            client_ip=client_ip, resource_type=resource_type, payload=payload,
        ):
            return response

        # Fire-and-forget — never block the response
        import asyncio
        asyncio.ensure_future(
            _write_audit(
                event_type=event_type,
                severity=severity,
                actor_user_id=actor_user_id,
                client_ip=client_ip,
                resource_type=resource_type,
                outcome=outcome,
                latency_ms=latency_ms,
                payload=payload,
            )
        )

        return response


# ── S2: refused repeats are counted, not written one by one ───────────────────
#
# Per process. A key's first refusal is written as it always was; every repeat
# inside REPEAT_WINDOW only counts. When the window ends, ONE summary row is
# written (`repeat_count` = how many it stands for, `details.first_at/last_at`).
# Append-only: no row is ever updated, so the audit stream forwards the summary
# like any other event, and counts are `SUM(repeat_count)`. Past MAX_REPEAT_KEYS
# distinct keys, a new key is written row by row as before — never dropped.
# A process that stops inside a window loses at most that window's count.
REPEAT_WINDOW = 60.0
MAX_REPEAT_KEYS = 10_000
_repeats: dict[tuple, dict] = {}


def _fold_repeat(key: tuple, **first) -> bool:
    """True = this refusal was counted into an open window (write nothing).
    False = write it now (and, if there is room, open a window for its key)."""
    import asyncio

    now = time.time()
    slot = _repeats.get(key)
    if slot is not None:
        slot["n"] += 1
        slot["last"] = now
        return True
    if len(_repeats) >= MAX_REPEAT_KEYS:
        return False
    from velaris_shared.observability.context import tenant_id_var
    _repeats[key] = {"n": 0, "first": now, "last": now,
                     "tenant_id": tenant_id_var.get(), **first}
    asyncio.ensure_future(_close_window(key))
    return False


async def _close_window(key: tuple) -> None:
    import asyncio
    from datetime import datetime, timezone

    await asyncio.sleep(REPEAT_WINDOW)
    slot = _repeats.pop(key, None)
    if not slot or slot["n"] <= 0:
        return
    try:
        from velaris_shared.db.security_events import log_security_event
        from velaris_shared.db.session import get_session_factory

        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        details = dict(slot["payload"], repeats=slot["n"],
                       first_at=iso(slot["first"]), last_at=iso(slot["last"]))
        async with get_session_factory()() as session:
            await log_security_event(
                session,
                event_type=slot["event_type"], severity=slot["severity"],
                user_id=slot["actor_user_id"], resource_type=slot["resource_type"],
                action=slot["payload"]["method"], outcome="denied",
                ip_address=slot["client_ip"], details=details,
                tenant_id=slot["tenant_id"], repeat_count=slot["n"],
            )
            await session.commit()
    except Exception:
        logger.debug("AuditMiddleware: repeat summary write failed (non-critical)", exc_info=True)


#: Query parameters that carry a secret: a socket's access token, a document
#: grant, a sign-in code. Their values never reach the audit record, which is
#: kept for good (2026-10-19).
_SECRET_QUERY_KEYS = frozenset({
    "token", "access_token", "refresh_token", "grant", "code",
    "sig", "signature", "key", "api_key", "apikey", "password", "secret",
})


def _safe_query(query: str) -> str:
    """The query string with the values of secret-bearing parameters blanked."""
    from urllib.parse import parse_qsl, urlencode

    pairs = parse_qsl(query, keep_blank_values=True)
    return urlencode([(k, "[REDACTED]" if k.lower() in _SECRET_QUERY_KEYS else v)
                      for k, v in pairs], safe="[]")


async def _write_audit(
    event_type: str,
    severity: str,
    actor_user_id: str | None,
    client_ip: str | None,
    resource_type: str,
    outcome: str,
    latency_ms: int,
    payload: dict,
) -> None:
    """Write one audit entry to trace_events + security_events in one session."""
    try:
        from velaris_shared.db.session import get_session_factory
        from velaris_shared.hxstream.emitter import emit_trace
        from velaris_shared.db.security_events import log_security_event

        factory = get_session_factory()
        async with factory() as session:
            # HxStream trace event (live WebSocket feed + persistent DB)
            await emit_trace(
                event_type,
                payload,
                actor_user_id=actor_user_id,
                actor_ip=client_ip,
                latency_ms=latency_ms,
                session=session,
            )

            # Enterprise security event (visible in Security Events panel)
            await log_security_event(
                session,
                event_type=event_type,
                severity=severity,
                user_id=actor_user_id,
                resource_type=resource_type,
                action=payload["method"],
                outcome=outcome,
                ip_address=client_ip,
                details=payload,
            )

            await session.commit()
    except Exception:
        logger.debug("AuditMiddleware: write failed (non-critical)", exc_info=True)
