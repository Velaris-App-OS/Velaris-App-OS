"""Call case-service's internal API from another Velaris service.

Straight to case-service — loopback on a single box, or
VELARIS_CASE_SERVICE_INTERNAL_URL — never through the gateway (which refuses
`/internal`). Sends the internal secret, and the workload-identity header when
that is configured. With internal TLS on, presents this service's own
certificate and verifies case-service against the install's CA (mutual TLS, as
every service port requires then).

Every internal route is idempotent, so a call that failed on the network is
retried once. Anything else raises InternalCallError: the caller decides what a
failure means for its own work (and most have a sweep that finishes it later).

**Local mode.** While the calling code still runs INSIDE case-service (before the
commerce move), the handler is registered in this very process
(`register_local`). The call then runs it directly with the CALLER'S session —
one transaction, exactly as before the split — instead of an HTTP call to
itself, which would hold one pooled connection while waiting for a second (the
nested-connection deadlock). The request model still validates the payload and
the answer still round-trips through JSON, so the contract is the same either way.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from velaris_shared.config import get_settings
from velaris_shared.internal.auth import HEADER

log = logging.getLogger(__name__)

PREFIX = "/internal/v1"
DEFAULT_TIMEOUT = 30.0

#: Tests point this at case-service's ASGI app (httpx.ASGITransport).
_transport: httpx.AsyncBaseTransport | None = None
#: path -> async handler(session, payload) registered by case-service itself.
_LOCAL: dict[str, Any] = {}
#: Tests set this to exercise the HTTP path even inside case-service.
force_http = False


def register_local(path: str, handler) -> None:
    """case-service registers each internal handler for in-process callers."""
    _LOCAL[path.lstrip("/")] = handler


class InternalCallError(RuntimeError):
    """The internal call did not succeed. `status` is None on a network failure."""

    def __init__(self, path: str, status: int | None, detail: str):
        super().__init__(f"internal call {path} failed ({status or 'network'}): {detail}")
        self.path, self.status, self.detail = path, status, detail


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Route internal calls through `transport` (tests); None restores the network."""
    global _transport  # noqa: PLW0603
    _transport = transport


def _tls():
    s = get_settings()
    if not s.internal_tls_enabled:
        return "http", None, True
    d = Path(s.internal_tls_dir)
    me = os.environ.get("VELARIS_SERVICE_NAME", "case-service")
    cert = (str(d / f"{me}.crt"), str(d / f"{me}.key"))
    return "https", cert, str(d / "ca.crt")


def _ssl_context(cert, verify):
    """httpx 0.28 sends NO client certificate when given `verify=<path>` plus
    `cert=(…)` (found 2026-10-19: case-service hung up mid-handshake). An explicit
    context — the install's CA, this service's certificate — is what works."""
    if cert is None:
        return verify
    import ssl

    ctx = ssl.create_default_context(cafile=verify)
    ctx.load_cert_chain(*cert)
    return ctx


def base_url() -> str:
    s = get_settings()
    if s.case_service_internal_url:
        return s.case_service_internal_url.rstrip("/")
    scheme, _, _ = _tls()
    return f"{scheme}://127.0.0.1:{os.environ.get('VELARIS_CASE_PORT', '8201')}"


async def call(path: str, payload: dict[str, Any], *, session=None,
               timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Ask case-service to do `path` with `payload`; return its JSON answer.

    `session`: the caller's session, used in local mode (same process) by every
    handler that reads or writes the database.
    """
    local = _LOCAL.get(path.lstrip("/"))
    # In-process whenever the handler lives here; a handler that touches the
    # database is always called with the caller's session.
    if local is not None and not force_http:
        import json

        from fastapi import HTTPException

        try:
            answer = await local(session, json.loads(json.dumps(payload, default=str)))
        except HTTPException as exc:
            raise InternalCallError(f"{PREFIX}/{path}", exc.status_code, str(exc.detail)) from exc
        return json.loads(json.dumps(answer, default=str))
    s = get_settings()
    if not s.internal_service_secret:
        raise InternalCallError(path, None, "VELARIS_INTERNAL_SERVICE_SECRET is not set")
    headers = {HEADER: s.internal_service_secret}
    if s.gateway_identity_secret:
        headers["X-Velaris-Gateway"] = s.gateway_identity_secret
    url = f"{PREFIX}/{path.lstrip('/')}"
    if _transport is not None:
        kw: dict[str, Any] = {"transport": _transport, "base_url": "http://case-service"}
    else:
        _, cert, verify = _tls()
        kw = {"base_url": base_url(), "verify": _ssl_context(cert, verify)}
    # Serialised the way local mode does (`default=str`: UUIDs, datetimes), so
    # the two paths carry the same JSON.
    import json

    body = json.dumps(payload, default=str).encode()
    headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=timeout, **kw) as client:
                resp = await client.post(url, content=body, headers=headers)
        except httpx.TransportError as exc:          # network: the route never answered
            last = exc
            log.warning("internal call %s: network error (attempt %d): %s", url, attempt, exc)
            continue
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:                         # noqa: BLE001
                detail = resp.text
            raise InternalCallError(url, resp.status_code, str(detail)[:500])
        return resp.json()
    raise InternalCallError(url, None, str(last))
