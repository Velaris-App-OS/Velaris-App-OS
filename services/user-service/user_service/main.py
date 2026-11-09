"""user-service — sign-in and identity in its own process (R5e step 10).

Serves, behind the gateway exactly as before (no client URL changed):

    /api/v1/auth                    sign-in (legacy shape), /me, /roles
    /api/v1/auth/real               login, refresh, logout, register, users,
                                    profile, passwords, MFA, WebAuthn, devices,
                                    SSO (OIDC) providers, token expiry
    /api/v1/auth/saml               the SAML service provider
    /api/v1/scim                    SCIM 2.0 provisioning + its tokens
    /api/v1/identity/provisioning   group mappings, links, the provisioning log
    /api/v1/user-directory          the user directory

`POST /api/v1/auth/switch-context` is NOT here: it is case-service's
(`access_groups.py`), and the gateway sends that one path there.

**Every service still checks a token itself** (`velaris_shared.auth`): this
service issues them, nothing asks it per request. Down, nobody can sign in or
refresh; a signed-in user keeps working until the access token expires.

**What it may not do itself:** send mail — the password-reset code goes through
case-service's internal API (`/internal/v1/user/otp-email`). It never imports
case-service — that is test-enforced.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

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

from user_service.api.routers.auth import router as auth_router
from user_service.api.routers.auth_real import router as auth_real_router
from user_service.api.routers.auth_saml import router as auth_saml_router
from user_service.api.routers.identity_admin import router as identity_admin_router
from user_service.api.routers.scim import router as scim_router
from user_service.api.routers.user_directory import router as user_directory_router

logger = logging.getLogger("user_service")

SERVICE_NAME = "user-service"
API_PREFIX = "/api/v1"


def _lifespan():
    """The platform lifespan (superadmin gate, authz + change-counter polls), plus
    what this service's code reads from memory — the tenant DEKs (MFA secrets, SSO
    client secrets and SAML SP keys are encrypted with them) and the feature flags —
    and its background jobs: the SAML metadata refresh, under the same job
    lease it had in case-service, so an install mid-update never runs it twice,
    and the purge of deleted accounts past their grace period (`user-purge`)."""
    base = platform_lifespan(SERVICE_NAME)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with base(app):
            from velaris_shared import hxvault
            from velaris_shared.core.feature_flags import refresh_version_cache
            from velaris_shared.db.session import get_session_factory
            from velaris_shared.jobs.leases import coroutine_job, run_singleton

            try:
                async with get_session_factory()() as session:
                    await hxvault.warm_cache(session)
                    await refresh_version_cache(session)
            except Exception as exc:                            # noqa: BLE001
                logger.warning("user-service: start-up cache fill failed: %s", exc)

            tasks: list[asyncio.Task] = []
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
                tasks.append(asyncio.create_task(_vault_resync_loop()))

            # If it never starts, providers keep the certificates they already
            # have and sign-in is unaffected — the sweep only ever *adds* a
            # rollover certificate early.
            try:
                from user_service.identity.saml_metadata import saml_metadata_cron
                tasks.append(asyncio.create_task(
                    run_singleton("saml-metadata", coroutine_job(saml_metadata_cron)),
                    name="lease:saml-metadata"))
                logger.info("SAML metadata refresh scheduled")
            except Exception as exc:                            # noqa: BLE001
                logger.warning("SAML metadata refresh unavailable, certificates will "
                               "not auto-renew: %s", exc)
            # Deleted accounts are purged — with everything that grants access —
            # once their grace period has passed (Nov_TODO "User deletion v2").
            # If it never runs they stay deleted (refused everywhere), just not
            # purged; nothing is let in.
            try:
                from velaris_shared.identity.lifecycle import purge_due_cron
                tasks.append(asyncio.create_task(
                    run_singleton("user-purge", coroutine_job(purge_due_cron)),
                    name="lease:user-purge"))
                logger.info("Deleted-account purge scheduled")
            except Exception as exc:                            # noqa: BLE001
                logger.warning("Deleted-account purge unavailable: %s", exc)
            try:
                yield
            finally:
                for t in tasks:
                    t.cancel()
                # Let the lease supervisor release its row on the way out.
                await asyncio.gather(*tasks, return_exceptions=True)

    return lifespan


def _error_handlers(app: FastAPI) -> None:
    """The answers case-service gave these routes and the platform set does not:
    SCIM's own error shape, and the vault's 410 / 503."""
    from velaris_shared.core.error_codes import (
        VAULT_DEK_503,
        VAULT_SHREDDED_410,
        error_payload,
    )
    from velaris_shared.hxvault import DekNotLoadedError
    from velaris_shared.hxvault.keyring import KeyShreddedError

    from user_service.identity.scim import ScimError

    log = logging.getLogger("user_service.errors")

    # SCIM speaks its own error shape (RFC 7644 §3.12) and its clients parse it:
    # Entra and Okta surface `detail` and `scimType` to the administrator and
    # branch on `status`. A refusal raised deep in the SCIM dependency chain —
    # the bearer-token check especially — must arrive in that shape rather than
    # as the platform's generic error body, or the IdP shows its operator a
    # blank failure.
    @app.exception_handler(ScimError)
    async def _scim_error_handler(request, exc: ScimError):  # noqa: ANN001
        log.warning("%s %s -> scim %s %s", request.method, request.url.path,
                    exc.status, exc.scim_type or "")
        return JSONResponse(status_code=exc.status, content=exc.body(),
                            media_type="application/scim+json")

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
        title="Velaris user-service",
        version="0.1.0",
        description="Sign-in and identity: login, MFA, SSO, SAML, SCIM, the user directory.",
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        lifespan=_lifespan(),
        # The same app-wide guard case-service applies: a path, query or body id
        # naming another tenant's row is answered 404.
        dependencies=[Depends(tenant_path_guard)],
    )

    # Commit each request's database work BEFORE its response is sent. Must be
    # set before any include_router below. See velaris_shared/db/commit_route.py.
    install_commit_before_response(app)

    # Same order as case-service included them: `/auth` first, so its `/auth/me`
    # is the one that answers (case-service's access_groups.py defines another,
    # which was never reached).
    for router in (auth_router, user_directory_router, auth_real_router,
                   auth_saml_router, scim_router, identity_admin_router):
        app.include_router(router, prefix=API_PREFIX)
    # Demo tenants for the website's sign-up (2026-11-04). Not under /api/v1 —
    # the gateway never forwards /internal; the routes refuse any call without
    # the demo provisioning secret (VELARIS_DEMO_PROVISION_SECRET).
    from user_service.internal_api import router as _internal_router
    app.include_router(_internal_router)

    mount_metrics(app)
    apply_platform_error_handlers(app, "user_service.errors")
    _error_handlers(app)
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
