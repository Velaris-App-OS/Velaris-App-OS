"""commerce-service — the commerce domain in its own process (R5e step 7).

Serves five prefixes, behind the gateway exactly as before (no client URL changed):

    /api/v1/checkout      HxCheckout — orders, intake tokens, webhooks
    /api/v1/payments      payment requests, connectors, disbursements, webhooks
    /api/v1/storefront    HxStorefront — stores, catalogue, public shop
    /api/v1/marketplace   the marketplace — packages, packs, grants, conformance
    /api/v1/model-market  F-07 — the sovereign model exchange

**What it may not do itself.** Opening an order's case, advancing a case,
applying a pack's bundle, the egress policy and HxSentinel are case-service's.
It asks over case-service's internal API (`velaris_shared.internal.client`),
straight to case-service with the install's internal service secret. It never
imports case-service — that is test-enforced.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from velaris_shared.app import (
    apply_platform_error_handlers,
    apply_platform_middleware,
    mount_metrics,
    platform_lifespan,
)
from velaris_shared.config import get_settings
from velaris_shared.db.commit_route import install_commit_before_response
from velaris_shared.observability import configure_logging, configure_telemetry
from velaris_shared.tenancy.path_guard import tenant_path_guard

from commerce_service.api.routers.checkout import router as checkout_router
from commerce_service.api.routers.marketplace import router as marketplace_router
from commerce_service.api.routers.model_market import router as model_market_router
from commerce_service.api.routers.payments import router as payments_router
from commerce_service.api.routers.storefront import router as storefront_router
from commerce_service.api.routers.storefront_public import router as storefront_public_router

logger = logging.getLogger("commerce_service")

SERVICE_NAME = "commerce-service"
API_PREFIX = "/api/v1"



def _lifespan():
    """The platform lifespan (superadmin gate, authz + change-counter polls), plus
    what this service's code reads from memory: the tenant DEKs (payment
    credentials are decrypted synchronously) and the released-feature flags."""
    base = platform_lifespan(SERVICE_NAME)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with base(app):
            from velaris_shared import hxvault
            from velaris_shared.core.feature_flags import refresh_version_cache
            from velaris_shared.db.session import get_session_factory

            try:
                async with get_session_factory()() as session:
                    await hxvault.warm_cache(session)
                    await refresh_version_cache(session)
            except Exception as exc:                            # noqa: BLE001
                logger.warning("commerce-service: start-up cache fill failed: %s", exc)

            resync_task = None
            interval = get_settings().vault_cache_resync_seconds
            if interval and interval > 0:
                async def _vault_resync_loop():
                    while True:
                        await asyncio.sleep(interval)
                        try:
                            async with get_session_factory()() as session:
                                await hxvault.resync_cache(session)
                        except Exception as exc:                # noqa: BLE001
                            logger.warning("HxVault resync failed: %s", exc)
                resync_task = asyncio.create_task(_vault_resync_loop())
            try:
                yield
            finally:
                if resync_task is not None:
                    resync_task.cancel()

    return lifespan


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Velaris commerce-service",
        version="0.1.0",
        description="Commerce: checkout, payments, storefront, marketplace, model market.",
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        lifespan=_lifespan(),
        # The same app-wide guard case-service applies: a path, query or body id
        # naming another tenant's row is answered 404. Commerce routes relied on
        # it before the move (marketplace access-rules / workspaces / updates,
        # and case_id / case_type_id / user_id / connector_id in bodies).
        dependencies=[Depends(tenant_path_guard)],
    )

    # Commit each request's database work BEFORE its response is sent. Must be
    # set before any include_router below. See velaris_shared/db/commit_route.py.
    install_commit_before_response(app)

    for router in (checkout_router, payments_router, storefront_router,
                   storefront_public_router, marketplace_router, model_market_router):
        app.include_router(router, prefix=API_PREFIX)

    mount_metrics(app)
    apply_platform_error_handlers(app, "commerce_service.errors")
    apply_platform_middleware(app)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "healthy", "service": SERVICE_NAME}

    @app.get("/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        from sqlalchemy import text

        from velaris_shared.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready", "service": SERVICE_NAME}

    configure_logging()
    try:
        configure_telemetry(SERVICE_NAME, app=app)
    except Exception:  # noqa: BLE001 - telemetry must never block startup
        logger.warning("telemetry not configured for %s", SERVICE_NAME, exc_info=True)

    return app


app = create_app()
