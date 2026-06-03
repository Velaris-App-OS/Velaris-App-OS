"""Tenant context — tracks the current tenant for the request.

Uses ContextVar so each async request has its own tenant scope.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class TenantContext:
    """Current tenant for this request."""
    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_name: str = ""
    role: str = "member"


_current_tenant: ContextVar[TenantContext | None] = ContextVar(
    "current_tenant", default=None
)


def set_current_tenant(tenant: TenantContext) -> None:
    _current_tenant.set(tenant)


def get_current_tenant() -> TenantContext | None:
    return _current_tenant.get()


def get_current_tenant_id() -> uuid.UUID | None:
    t = get_current_tenant()
    return t.tenant_id if t else None


def clear_current_tenant() -> None:
    _current_tenant.set(None)
