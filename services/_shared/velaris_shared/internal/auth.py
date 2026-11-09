"""The guard on case-service's `/internal/v1/*` routes.

A call is admitted only when it carries the install's internal service secret
(`X-Velaris-Internal`, constant-time compare) and did NOT come through a proxy:
the gateway never forwards `/internal`, and a request bearing the gateway's
forwarding headers is refused here too, so a gateway misconfiguration cannot
expose these routes. No secret configured → every internal route refuses (503).

There is no user on these calls. Each route carries its tenant explicitly and
checks it; the caller is trusted with platform authority because only another
Velaris service on this install holds the secret.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request

from velaris_shared.config import get_settings

log = logging.getLogger(__name__)

HEADER = "X-Velaris-Internal"
#: Headers only a proxy (the gateway, an ingress) adds. Their presence means the
#: call did not come straight from a sibling service.
_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def is_internal_caller(request) -> bool:
    """True when this request PROVES it is a sibling service: an /internal path,
    the right secret, and no proxy headers. Cheap and side-effect free — the
    per-address rate limit uses it so a sibling's calls (all from one address)
    are not throttled like a user's, while anything that cannot prove it is."""
    if not request.url.path.startswith("/internal/"):
        return False
    secret = get_settings().internal_service_secret or ""
    if not secret or any(h in request.headers for h in _PROXY_HEADERS):
        return False
    presented = request.headers.get(HEADER, "")
    return bool(presented) and hmac.compare_digest(presented.encode(), secret.encode())


async def require_internal_caller(request: Request) -> None:
    secret = get_settings().internal_service_secret or ""
    if not secret:
        log.warning("internal API refused %s: VELARIS_INTERNAL_SERVICE_SECRET is not set",
                    request.url.path)
        raise HTTPException(503, "The internal service API is not configured on this install.")
    if any(h in request.headers for h in _PROXY_HEADERS):
        log.warning("internal API refused %s: arrived through a proxy", request.url.path)
        raise HTTPException(404, "Not Found")
    presented = request.headers.get(HEADER, "")
    if not presented or not hmac.compare_digest(presented.encode(), secret.encode()):
        log.warning("internal API refused %s: missing or wrong internal secret", request.url.path)
        raise HTTPException(403, "Forbidden")


# ── The website's demo provisioning (2026-11-04) ──────────────────────────────
# Its own secret, accepted on /internal/v1/demo/* ONLY: the internet-facing
# website must not hold the internal service secret or the gateway identity
# secret, which together reach every /internal route for every tenant.
DEMO_HEADER = "X-Velaris-Demo"
DEMO_PREFIX = "/internal/v1/demo/"


def is_demo_caller(request) -> bool:
    """True when this request PROVES it is the website's provisioning call: a
    demo path, the demo secret, and no proxy headers. Side-effect free — the
    gateway-identity check lets exactly this through."""
    if not request.url.path.startswith(DEMO_PREFIX):
        return False
    secret = get_settings().demo_provision_secret or ""
    if not secret or any(h in request.headers for h in _PROXY_HEADERS):
        return False
    presented = request.headers.get(DEMO_HEADER, "")
    return bool(presented) and hmac.compare_digest(presented.encode(), secret.encode())


async def require_demo_caller(request: Request) -> None:
    secret = get_settings().demo_provision_secret or ""
    if not secret:
        log.warning("demo API refused %s: VELARIS_DEMO_PROVISION_SECRET is not set",
                    request.url.path)
        raise HTTPException(503, "Demo provisioning is not configured on this install.")
    if any(h in request.headers for h in _PROXY_HEADERS):
        log.warning("demo API refused %s: arrived through a proxy", request.url.path)
        raise HTTPException(404, "Not Found")
    presented = request.headers.get(DEMO_HEADER, "")
    if not presented or not hmac.compare_digest(presented.encode(), secret.encode()):
        log.warning("demo API refused %s: missing or wrong demo secret", request.url.path)
        raise HTTPException(403, "Forbidden")
