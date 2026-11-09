from __future__ import annotations
import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .context import request_id_var, tenant_id_var, trace_id_var, user_id_var
from .metrics import http_request_duration_seconds, http_requests_total
from .spans import record_span


def _path_template(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    return request.url.path


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        # Not the X-Tenant-Id header: the client sets it (2026-11-04). The
        # trusted tenant is set by sign-in (auth/dependencies.py) on
        # request.state and on tenant_id_var; until then a line has none.
        tenant_id = None
        user_id = request.headers.get("x-user-id")

        t_rid = request_id_var.set(request_id)
        t_tid = tenant_id_var.set(tenant_id)
        t_uid = user_id_var.set(user_id) if user_id else None

        try:
            from opentelemetry import trace as _otel_trace
            span = _otel_trace.get_current_span()
            ctx = span.get_span_context() if span else None
            if ctx and ctx.is_valid:
                trace_id_var.set(format(ctx.trace_id, "032x"))
        except Exception:
            pass

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            duration = time.perf_counter() - start
            path_tpl = _path_template(request)
            tenant_id = getattr(request.state, "velaris_tenant", None) or "-"
            try:
                # `tenant_id`: the signed-in caller's tenant (bounded by the
                # number of tenants), "-" before sign-in. It was the client's
                # X-Tenant-Id header until 2026-11-04 — unbounded and forgeable.
                http_requests_total.labels(
                    method=request.method, path=path_tpl,
                    status=str(status_code), tenant_id=tenant_id,
                ).inc()
                http_request_duration_seconds.labels(
                    method=request.method, path=path_tpl, tenant_id=tenant_id,
                ).observe(duration)
                record_span(
                    method=request.method, path=str(request.url.path),
                    status=status_code, duration_ms=round(duration * 1000.0, 3),
                    request_id=request_id, tenant_id=tenant_id,
                )
            except Exception:
                pass
            request_id_var.reset(t_rid)
            tenant_id_var.reset(t_tid)
            if t_uid is not None:
                user_id_var.reset(t_uid)
