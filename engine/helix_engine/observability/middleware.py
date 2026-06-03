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
        tenant_id = request.headers.get("x-tenant-id") or "default"
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
            try:
                http_requests_total.labels(
                    method=request.method, path=path_tpl,
                    status=str(status_code), tenant_id=tenant_id,
                ).inc()
                http_request_duration_seconds.labels(
                    method=request.method, path=path_tpl,
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
