"""The platform door — one middleware stack for every Velaris service.

Extraction's quietest failure mode is a *weaker second door*: the new process
answers the same authenticated routes as case-service but without its audit
trail, body limits, rate limit or security headers, and the gateway happily
routes traffic to it. This module exists so that cannot happen by omission —
an extracted service calls `apply_platform_middleware()` and gets the same
stack case-service applies, in the same order.

Order matters: the LAST middleware added is the FIRST executed, so
`SecurityHeadersMiddleware` is added last and its headers land on every
response, including the 413s and 429s produced by the layers beneath it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from starlette.middleware.cors import CORSMiddleware

from velaris_shared.config import get_settings
from velaris_shared.middleware.audit import AuditMiddleware
from velaris_shared.middleware.body_limit import BodyLimitMiddleware
from velaris_shared.middleware.gateway_identity import GatewayIdentityMiddleware
from velaris_shared.middleware.rate_limit import RateLimitMiddleware
from velaris_shared.middleware.request_tracking import RequestTrackingMiddleware
from velaris_shared.middleware.security_headers import SecurityHeadersMiddleware
from velaris_shared.middleware.superadmin_gate import SuperadminGateMiddleware
from velaris_shared.observability import ObservabilityMiddleware, render_metrics

__all__ = [
    "apply_platform_error_handlers",
    "apply_platform_middleware",
    "mount_metrics",
    "start_authz_loops",
    "platform_lifespan",
]

_log = logging.getLogger("velaris_shared.app")


def platform_lifespan(service_name: str):
    """The boot checks every service must run, whatever else it does at startup.

    Today that is one thing, and it is the one that must not be per-service: the
    **superadmin gate**. An install with no superadmin has not finished setup,
    and case-service answers 503 on everything until it has. A service extracted
    out of it that skipped the check would keep serving its own routes on that
    same unconfigured install — the gate would be true of the platform only
    where it happened to be wired, which is not what a gate is.

    It also starts the two background loops that keep this service's view of
    **authorization** current, which is not optional in a multi-service platform:

    * `role_privileges_cron` — the role → privilege store. Started only in
      case-service until the #16-C audit, which meant the store was permanently
      EMPTY in every extracted service: an admin granting a privilege in the
      Platform Roles tab saw it apply to case-service routes and silently do
      nothing anywhere else. Proven live on 2026-08-23 — the same grant took
      case-service from 403 to 200 while analytics-service stayed 403.
    * `generation_cron` — the shared invalidation signal, so a revoke made in
      one process reaches the decision caches of the others.

    Fails OPEN on a database error, exactly as case-service does: a service that
    refuses to start because the database was slow is worse than one that starts
    and finds out.
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        try:
            from sqlalchemy import func, select

            from velaris_shared.db.models import HelixUserModel
            from velaris_shared.db.session import get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                count = (await session.execute(
                    select(func.count()).select_from(HelixUserModel)
                    .where(HelixUserModel.is_superadmin == True)  # noqa: E712
                    .where(HelixUserModel.is_active == True)      # noqa: E712
                )).scalar_one()
            if count == 0:
                _log.critical(
                    "STARTUP BLOCKED (%s): no active superadmin account. Run "
                    "./setup-velaris.sh. This service will answer 503 on every "
                    "endpoint until one exists.", service_name,
                )
                app.state.superadmin_missing = True
            else:
                app.state.superadmin_missing = False
                _log.info("Superadmin gate (%s): verified", service_name)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "Superadmin gate check failed for %s (DB may not be ready): %s",
                service_name, exc,
            )
            app.state.superadmin_missing = False

        tasks = start_authz_loops(service_name)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    return _lifespan


def start_authz_loops(service_name: str) -> list:
    """Start the policy loaders. Returns the tasks so a shutdown can cancel them.

    Each in its own try: a service whose role store failed to schedule still
    runs, because the store is ADDITIVE — an empty one means "as before", never
    "nobody holds anything". Losing the generation poll only widens staleness
    back to the 30-second cache TTL. Neither is worth refusing to boot over, and
    both log loudly enough to find.
    """
    import asyncio

    tasks = []
    try:
        from velaris_shared.hxguard.role_store import role_privileges_cron
        tasks.append(asyncio.create_task(role_privileges_cron()))
        _log.info("Role privilege store refresh scheduled (%s)", service_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Role privilege store unavailable in %s, seeded privileges only: %s",
            service_name, exc,
        )
    try:
        from velaris_shared.hxguard.generation import generation_cron
        tasks.append(asyncio.create_task(generation_cron()))
        _log.info("Authz generation poll scheduled (%s)", service_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Authz generation poll unavailable in %s: %s", service_name, exc)
    # The same signal for every other cache that must agree between processes
    # (feature flags, revoked sessions, data keys, tenant lookups).
    try:
        from velaris_shared.generations import generations_cron
        tasks.append(asyncio.create_task(generations_cron()))
        _log.info("Platform generations poll scheduled (%s)", service_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Platform generations poll unavailable in %s: %s", service_name, exc)
    return tasks


def mount_metrics(app: FastAPI) -> None:
    """Expose this service's Prometheus metrics.

    `ObservabilityMiddleware` records them either way; without this endpoint
    they are recorded and never scrapeable, so an extracted service becomes a
    blind spot in the same dashboard that covers case-service. Unauthenticated
    and excluded from the schema, matching case-service's own `/metrics` — the
    services bind loopback and are reached only through the gateway.
    """

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)


def apply_platform_middleware(app: FastAPI) -> None:
    """Apply the standard stack. Mirrors case_service.main, minus its own gates.

    `SuperadminGateMiddleware` is included, and the service must pair it with
    `platform_lifespan()` — the middleware only reads a flag, so without the
    lifespan that sets it the gate is inert and reads as "configured".
    """
    settings = get_settings()

    # Added in case-service's order. Starlette inserts each new middleware at
    # the front, so the LAST added is the outermost — which is why
    # SecurityHeaders goes last and its headers land on 413s and 429s too.
    app.add_middleware(SuperadminGateMiddleware)
    app.add_middleware(ObservabilityMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestTrackingMiddleware)
    # R5d 11b: read-your-own-writes mark + where an opted-in read ran.
    from velaris_shared.middleware.read_routing import ReadRoutingMiddleware
    app.add_middleware(ReadRoutingMiddleware)

    # Per client ADDRESS, as the gateway (VELARIS_GW_RATE_API, 20 r/s burst 40)
    # now hands it over. Was 120/min burst 30 when every Studio user arrived as
    # 127.0.0.1 — on the Redis backend that was ONE bucket for the whole install
    # (R6, 2026-10-30). Matched to the gateway so this second layer is never the
    # stricter one: nothing that works today is refused by it.
    if not settings.debug and "PYTEST_CURRENT_TEST" not in os.environ:
        if (getattr(settings, "rate_limit_backend", "memory") == "redis"
                and getattr(settings, "redis_enabled", False)):
            from velaris_shared.middleware.rate_limit_redis import RedisRateLimitMiddleware
            app.add_middleware(
                RedisRateLimitMiddleware,
                requests_per_minute=1200, burst=40,
                exclude_paths=["/health", "/ready", "/metrics"],
            )
        else:
            app.add_middleware(
                RateLimitMiddleware,
                requests_per_minute=1200, burst=40,
                exclude_paths=["/health", "/ready"],
            )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.debug else [],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        BodyLimitMiddleware,
        max_body_bytes=settings.max_body_bytes,
        max_upload_bytes=settings.max_upload_bytes,
        max_migrate_upload_bytes=settings.max_migrate_upload_bytes,
    )
    # Added second to last, so it runs second: security headers land on the 403,
    # and a caller that did not come through the gateway is refused BEFORE the
    # audit middleware opens a database session on its behalf.
    app.add_middleware(
        GatewayIdentityMiddleware,
        secret=settings.gateway_identity_secret,
        service_name=app.title,
    )
    app.add_middleware(SecurityHeadersMiddleware)


def apply_platform_error_handlers(app: FastAPI, logger_name: str) -> None:
    """The JSON error contract, for services that do not build their own.

    Every response from a Velaris API is JSON — including the ones nobody
    classified. Starlette's default floor returns `text/plain`, which every
    client in this platform then fails to parse, so the real fault becomes a
    JSON syntax error in the browser. An extracted service that skipped this
    would break that contract for its own routes only, which is the hardest
    kind of inconsistency to notice.

    case-service registers these itself, along with the handlers for its own
    domains (SCIM's RFC 7644 shape, the vault's sealed-key 503, the documents
    UUID 422). Those stay where the domains are.
    """
    import logging

    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, PendingRollbackError

    from velaris_shared.core.error_codes import (
        DB_UNAVAILABLE_503,
        SYS_UNHANDLED_500,
        CodedHTTPException,
        error_payload,
    )

    log = logging.getLogger(logger_name)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request, exc: RequestValidationError):  # noqa: ANN001
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    @app.exception_handler(CodedHTTPException)
    async def _coded_error_handler(request, exc: CodedHTTPException):  # noqa: ANN001
        log.warning("%s %s -> %s %s", request.method, request.url.path, exc.status_code, exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.detail),
            headers=getattr(exc, "headers", None),
        )

    async def _db_unavailable_handler(request, exc):  # noqa: ANN001
        # The driver's message carries host, port and user. Log, never return.
        log.error("%s %s -> database unavailable: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=503,
            content=error_payload(
                DB_UNAVAILABLE_503,
                "The database is unavailable. This is not a problem with your request.",
            ),
        )

    # A tuple, for the reason recorded in case_service.main: SQLAlchemy only
    # wraps a fault into OperationalError once a connection exists.
    for _fault in (OperationalError, InterfaceError, ConnectionError):
        app.add_exception_handler(_fault, _db_unavailable_handler)

    # A lost connection that a route's `except Exception` swallowed surfaces
    # later as PendingRollbackError on the next query (R6 failure test,
    # 2026-11-03: PostgreSQL killed mid-create answered 500). Nothing was
    # committed, so it is the same "database unavailable". Any other pending
    # rollback (e.g. a swallowed IntegrityError) is a real bug and stays a 500.
    @app.exception_handler(PendingRollbackError)
    async def _pending_rollback_handler(request, exc):  # noqa: ANN001
        if "Can't reconnect until invalid transaction is rolled back" in str(exc):
            return await _db_unavailable_handler(request, exc)
        return await _unhandled_handler(request, exc)

    # A connection dropped mid-query (the pooler or the database killed under
    # load) can reach us as a plain DBAPIError — asyncpg's
    # ConnectionDoesNotExistError has no closer SQLAlchemy class (R6 failure
    # test, 2026-11-03: GET /cases answered 500). SQLAlchemy marks those
    # `connection_invalidated`; that is "database unavailable". Any other
    # DBAPIError is a real fault and stays a 500. OperationalError and
    # InterfaceError keep their own handler above (more specific class wins).
    @app.exception_handler(DBAPIError)
    async def _dbapi_handler(request, exc):  # noqa: ANN001
        if getattr(exc, "connection_invalidated", False):
            return await _db_unavailable_handler(request, exc)
        return await _unhandled_handler(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request, exc):  # noqa: ANN001
        log.error(
            "%s %s -> unhandled %s", request.method, request.url.path,
            type(exc).__name__, exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                SYS_UNHANDLED_500,
                "The server hit an unexpected error. It has been logged with a "
                "request id; your request was not applied.",
            ),
        )
