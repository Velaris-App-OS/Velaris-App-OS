"""Workload identity — only the gateway may call a service (roadmap #18).

**The gap this closes.** Since 13a the gateway has been "the only door", but
that was a statement about *routing*. Each service binds `127.0.0.1`, and any
other process on the host — a compromised bot container with host access, a
misconfigured cron, a developer's script, anything running as any user — could
open `127.0.0.1:8202` directly and reach an authenticated API without passing
through the gateway at all. Loopback is a network boundary, not an identity.

**What it does.** The gateway injects a shared credential on every proxied
request. A service with `gateway_identity_secret` configured refuses anything
that does not carry it. `proxy_set_header` *replaces* whatever the client sent,
so a caller cannot smuggle their own value through the gateway, and a caller
going around the gateway does not know it.

**Why a shared secret rather than a signed, short-lived token.** The gateway is
stock `nginx:1.27-alpine`, which cannot mint or sign anything without njs or
lua. A rotating secret in a file both sides read is what this deployment can
actually enforce today; the transport-level version of the same idea is mTLS,
whose plumbing ships beside this and is off by default. The secret is compared
in constant time and never logged.

**Exemptions, and why each one is safe.** `/health`, `/ready` and `/metrics`
are reached directly and legitimately — by systemd's `ExecStartPre`, by Docker
health checks, and by a metrics scrape. None of them reads tenant data; the
first two return a fixed shape and `/metrics` is already an anonymous endpoint
on case-service. `/internal/v1/demo/*` carrying the demo provisioning secret
(`velaris_shared.internal.auth.is_demo_caller`) is the website's one direct
call; that secret opens nothing else. Everything else is refused.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

#: The header the gateway injects. `X-` prefixed on purpose: it is a hop-by-hop
#: convention here, not an IANA-registered field.
HEADER = "X-Velaris-Gateway"

#: Reached directly by supervisors and scrapes, never carrying tenant data.
EXEMPT_PATHS = frozenset({"/health", "/ready", "/metrics"})


class GatewayIdentityMiddleware(BaseHTTPMiddleware):
    """Refuse anything that did not come through the gateway.

    Inert when no secret is configured — an upgrade must not lock an existing
    install out of its own API. `setup-velaris.sh` generates one for new
    installs, and the settings docstring says why it matters.
    """

    def __init__(self, app, secret: str = "", service_name: str = ""):
        super().__init__(app)
        self._secret = secret or ""
        self._service = service_name or "service"
        if not self._secret:
            log.warning(
                "Workload identity is OFF for %s: VELARIS_GATEWAY_IDENTITY_SECRET "
                "is unset, so any local process can reach this service directly "
                "instead of through the gateway.", self._service,
            )

    async def dispatch(self, request: Request, call_next):
        if not self._secret or request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        # The website's demo provisioning (2026-11-04) holds its own secret,
        # valid on /internal/v1/demo/* only — never the gateway's.
        from velaris_shared.internal.auth import is_demo_caller
        if is_demo_caller(request):
            return await call_next(request)

        presented = request.headers.get(HEADER, "")
        # compare_digest on the raw strings: not `==`, which returns early on the
        # first differing byte and leaks the prefix to a patient caller.
        if not presented or not hmac.compare_digest(presented, self._secret):
            log.warning(
                "%s refused a request that did not come through the gateway: %s %s",
                self._service, request.method, request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "This service is reachable only through the Velaris API "
                        "gateway."
                    ),
                    "error": {"code": "VEL-SYS-GATEWAY-REQUIRED-403"},
                },
            )
        return await call_next(request)
