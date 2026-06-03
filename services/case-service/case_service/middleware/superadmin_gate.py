"""SuperadminGateMiddleware — returns 503 on all endpoints if no superadmin exists.

Set on app.state.superadmin_missing = True by the lifespan hook.
Health endpoints are exempt so Docker / load balancers can still probe.
"""
from __future__ import annotations

import json
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_EXEMPT = {"/health", "/ready", "/metrics", "/docs", "/openapi.json", "/redoc"}


class SuperadminGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if getattr(request.app.state, "superadmin_missing", False):
            if request.url.path not in _EXEMPT:
                return Response(
                    content=json.dumps({
                        "detail": (
                            "Service not configured: no superadmin account found. "
                            "Run ./setup-velaris.sh to complete setup."
                        )
                    }),
                    status_code=503,
                    media_type="application/json",
                )
        return await call_next(request)
