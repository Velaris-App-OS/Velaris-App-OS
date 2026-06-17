"""Security response headers middleware (D1).

Adds defense-in-depth browser headers to every response:

  X-Content-Type-Options    — block MIME sniffing
  X-Frame-Options           — block clickjacking via framing
  Referrer-Policy           — never leak URLs to third parties
  Permissions-Policy        — disable powerful browser features
  Strict-Transport-Security — force HTTPS once seen over TLS
                              (ignored by browsers on plain HTTP, so safe in dev)
  Content-Security-Policy   — `default-src 'none'` for API (JSON) responses;
                              relaxed to `frame-ancestors 'none'` for HTML
                              responses (the /graph/visualize page uses inline
                              scripts and would break under a strict CSP)

Uses setdefault throughout — an endpoint that sets its own header wins.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers

        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )

        content_type = headers.get("content-type", "")
        if content_type.startswith("text/html"):
            headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        else:
            headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'",
            )

        return response
