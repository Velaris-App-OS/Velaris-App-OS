from __future__ import annotations
import contextvars
from typing import Optional

request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("helix_request_id", default=None)
trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("helix_trace_id", default=None)
tenant_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("helix_tenant_id", default=None)
user_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("helix_user_id", default=None)


def get_context() -> dict:
    return {
        "request_id": request_id_var.get(),
        "trace_id": trace_id_var.get(),
        "tenant_id": tenant_id_var.get(),
        "user_id": user_id_var.get(),
    }
