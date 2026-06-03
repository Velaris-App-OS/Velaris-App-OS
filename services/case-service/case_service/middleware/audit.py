"""AuditMiddleware — system-wide action logging for every mutating request.

Intercepts every POST/PUT/PATCH/DELETE request and writes one entry to
both trace_events (feeds HxStream live WebSocket) and security_events
(feeds Enterprise > Security Events panel).

This single middleware covers the ~50 routers that don't call emit_trace
manually, giving admins full visibility into every action in the system.
Login/logout are handled explicitly in auth_real.py rather than here
(the middleware has no actor until after login succeeds).

Copyright (c) 2024-2025 HELIX Contributors
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
        from case_service.auth.jwt_handler import decode_jwt_token
        from case_service.config import get_settings
        claims = decode_jwt_token(token, secret=get_settings().auth_secret)
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
            "query":        str(request.url.query) if request.url.query else None,
        }

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
        from case_service.db.session import get_session_factory
        from case_service.hxstream.emitter import emit_trace
        from case_service.enterprise.security_events import log_security_event

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
