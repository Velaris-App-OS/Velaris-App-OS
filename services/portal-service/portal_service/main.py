"""portal-service — the customer portal in its own process (R5e step 8).

Serves two prefixes, behind the gateway exactly as before (no client URL changed):

    /api/v1/portal        the public portal — submit, track, customer accounts
                          (OTP sign-in, export, self-erasure), case actions,
                          messages, chat, meetings, voice
    /api/v1/portal-admin  Studio's side — portal settings, case-type display,
                          customer management, submissions

**What it may not do itself.** Opening a portal case (rollouts, consent),
completing a customer step (audit, outbound rules, auto-advance), reading case
variables, meeting tokens, GDPR export / erasure (legal hold), speech
recognition and voice form-fill, and sending a sign-in code are case-service's.
It asks over case-service's internal API (`velaris_shared.internal.client`;
handlers in `case_service/internal_api/portal.py`), straight to case-service
with the install's internal service secret. It never imports case-service —
that is test-enforced.

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

from portal_service.api.routers.portal import admin_router as portal_admin_router
from portal_service.api.routers.portal import public_router as portal_router
from portal_service.api.routers.portal_customers import (
    admin_router as portal_customers_admin_router,
)
from portal_service.api.routers.portal_customers import (
    public_router as portal_customers_public_router,
)
from portal_service.api.routers.portal_voice import router as portal_voice_router

logger = logging.getLogger("portal_service")

SERVICE_NAME = "portal-service"
API_PREFIX = "/api/v1"


def _lifespan():
    """The platform lifespan (superadmin gate, authz + change-counter polls), plus
    what this service's code reads from memory: the tenant DEKs (bank details and
    uploaded documents are encrypted with them) and the released-feature flags
    (customer accounts, voice)."""
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
                logger.warning("portal-service: start-up cache fill failed: %s", exc)

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


def _vault_error_handlers(app: FastAPI) -> None:
    """The vault's two answers, as case-service gives them: a destroyed key is
    410 (never coming back), an unavailable one 503 (retry once the vault is
    fixed). Without them both would be a 500 here."""
    from fastapi.responses import JSONResponse

    from velaris_shared.core.error_codes import (
        VAULT_DEK_503,
        VAULT_SHREDDED_410,
        error_payload,
    )
    from velaris_shared.hxvault import DekNotLoadedError
    from velaris_shared.hxvault.keyring import KeyShreddedError

    log = logging.getLogger("portal_service.errors")

    @app.exception_handler(KeyShreddedError)
    async def _vault_shredded_handler(request, exc):  # noqa: ANN001
        log.error("%s %s -> shredded DEK: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=410,
            content=error_payload(
                VAULT_SHREDDED_410,
                "This data was encrypted with a key that has been destroyed "
                "(crypto-shredding). It cannot be recovered — not from a backup.",
            ),
        )

    @app.exception_handler(DekNotLoadedError)
    async def _vault_dek_handler(request, exc):  # noqa: ANN001
        log.error("%s %s -> DEK unavailable: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=503,
            content=error_payload(
                VAULT_DEK_503,
                "Encryption keys are unavailable. The vault may be sealed or "
                "unreachable; the data itself is intact.",
            ),
        )


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Velaris portal-service",
        version="0.1.0",
        description="The customer portal: public portal, customer accounts, portal admin.",
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        lifespan=_lifespan(),
        # The same app-wide guard case-service applies: a path, query or body id
        # naming another tenant's row is answered 404. The portal-admin routes
        # relied on it before the move (case_type_id in the path).
        dependencies=[Depends(tenant_path_guard)],
    )

    # Commit each request's database work BEFORE its response is sent. Must be
    # set before any include_router below. See velaris_shared/db/commit_route.py.
    install_commit_before_response(app)

    # Same order as case-service included them: the account routes share the
    # `/portal/{slug}/…` space with the public ones.
    for router in (portal_router, portal_admin_router, portal_customers_public_router,
                   portal_customers_admin_router, portal_voice_router):
        app.include_router(router, prefix=API_PREFIX)

    mount_metrics(app)
    apply_platform_error_handlers(app, "portal_service.errors")
    _vault_error_handlers(app)
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
