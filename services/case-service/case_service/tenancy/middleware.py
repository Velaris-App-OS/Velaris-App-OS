"""Tenant resolution middleware.

Determines the current tenant from:
  1. X-Tenant-Slug or X-Tenant-Id header
  2. Authenticated user's default tenant
  3. Falls back to 'default' tenant

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from case_service.tenancy.context import TenantContext, set_current_tenant, clear_current_tenant

logger = logging.getLogger(__name__)

# Paths that don't need tenant resolution
EXEMPT_PATHS = {
    "/health", "/ready", "/docs", "/openapi.json", "/redoc",
    "/api/v1/auth/login", "/api/v1/auth/me", "/api/v1/auth/roles",
    "/api/v1/tenants",  # Tenant management itself is tenant-less
}


class TenantMiddleware(BaseHTTPMiddleware):
    """Resolves tenant from request and sets context."""

    async def dispatch(self, request: Request, call_next):
        # Exempt paths skip tenant resolution
        path = request.url.path
        if any(path.startswith(exempt) for exempt in EXEMPT_PATHS):
            return await call_next(request)

        # Get tenant hint from header
        slug = request.headers.get("X-Tenant-Slug", "default")

        # Resolve tenant
        from case_service.db.session import get_session_factory
        from case_service.tenancy import repository as tenant_repo

        factory = get_session_factory()
        try:
            async with factory() as session:
                tenant = await tenant_repo.get_tenant_by_slug(session, slug)
                if tenant is None:
                    # Fall back to default
                    tenant = await tenant_repo.get_tenant_by_slug(session, "default")

                if tenant is None:
                    # No default tenant — tenancy not set up yet, allow through
                    logger.debug("No tenant found — skipping tenant context")
                    return await call_next(request)

                ctx = TenantContext(
                    tenant_id=tenant.id,
                    tenant_slug=tenant.slug,
                    tenant_name=tenant.name,
                )
                set_current_tenant(ctx)
        except Exception as e:
            logger.warning("Tenant resolution failed: %s", e)
            # Continue without tenant context rather than blocking
            return await call_next(request)

        try:
            response = await call_next(request)
            response.headers["X-Tenant-Slug"] = slug
            return response
        finally:
            clear_current_tenant()
