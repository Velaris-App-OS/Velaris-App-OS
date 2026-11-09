"""Velaris Case Service — FastAPI application.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from case_service.api.routers import (
    assignments,
    case_config,
    case_tenant_transfers,
    case_types,
    cases,
    legal_holds,
    data_models,
    forms,
    my_work,
    queues,
    rules,
    sla,
)

from velaris_shared.db.commit_route import install_commit_before_response
from velaris_shared.observability import (
    ObservabilityMiddleware, configure_logging, configure_telemetry,
)
from case_service.api.observability import router as observability_router
from case_service.api.routers.form_submissions import router as form_submissions_router
from case_service.api.routers.webhooks import router as webhooks_router
from case_service.api.routers.admin import router as admin_router
from case_service.api.routers.nlp import router as nlp_router
from case_service.api.routers.tenants import router as tenants_router
from case_service.api.routers.codegen import router as codegen_router
from case_service.api.routers.enterprise import router as enterprise_router
from case_service.api.routers.sitemap import router as sitemap_router
from case_service.api.routers.realtime import router as realtime_router
from case_service.api.routers.documents import router as documents_router
from case_service.api.routers.escalation import router as escalation_router
from case_service.api.routers.compliance import router as compliance_router
from case_service.api.routers.email import router as email_router
from case_service.api.routers.push import router as push_router
from case_service.api.routers.hxnexus import router as hxnexus_router
from case_service.api.routers.access_groups import router as access_groups_router
from case_service.api.routers.hxstream import router as hxstream_router
from case_service.api.routers.status import router as status_router
from case_service.api.routers.knowledge import router as knowledge_router
from case_service.api.routers.apps import router as apps_router
from case_service.api.routers.hxbridge import router as hxbridge_router, webhook_router
from case_service.api.routers.hxsync import router as hxsync_router
from case_service.api.routers.audit_stream import router as audit_stream_router
from case_service.api.routers.hxglobal import router as hxglobal_router
from case_service.api.routers.hxshield import router as hxshield_router
from case_service.api.routers.hxfusion import router as hxfusion_router
from case_service.api.routers.kyc import identity_router, esign_router
from case_service.api.routers.crm import crm_router, invoice_router
from case_service.api.routers.comms import router as comms_router
from case_service.api.routers.docintel import router as docintel_router
from case_service.api.routers.devconn import router as devconn_router
from case_service.api.routers.calendar import router as calendar_router      # Calendar P1
from case_service.api.routers.arbiter import router as arbiter_router        # HxArbiter ARB-A
from case_service.api.routers.agents import router as agents_router          # F-00 agent identity
from case_service.api.routers.agents import workforce_router as agents_workforce_router  # F-05
from case_service.api.routers.capture import router as capture_router  # F-06 ambient capture
from case_service.api.routers.ai_ledger import router as ai_ledger_router    # F-01 AI decision ledger
from case_service.api.routers.healing import router as healing_router  # F-08 self-healing processes
from case_service.api.routers.handoff import router as handoff_router  # F-09 agent handoff
from case_service.api.routers.field import router as field_router  # F-10 field operations
from case_service.api.routers.rollouts import router as rollouts_router  # Oct 18a staged rollouts
from case_service.api.routers.intent import router as intent_router  # F-11 intent execution
from case_service.api.routers.negotiate import router as negotiate_router  # F-12 negotiation
from case_service.api.routers.engage import router as engage_router  # item 12 rep desk
from case_service.api.routers.proofs import router as proofs_router          # F-03 compliance proofs
from case_service.api.routers.hxdeploy import router as hxdeploy_router
from case_service.api.routers.hxwork import router as hxwork_router
from case_service.api.routers.hxcanvas import router as hxcanvas_router
from case_service.api.routers.hxdocs import router as hxdocs_router
from case_service.api.routers.branches import router as branches_router   # P60 HxBranch
from case_service.api.routers.intake   import router as intake_router     # Process-Case integration
from case_service.api.routers.commits   import router as commits_router    # P61 Commit pattern
from case_service.api.routers.hxlogs   import router as hxlogs_router     # P63 HxLogs
from case_service.api.routers.channels import router as channels_router  # Omnichannel inbound
from case_service.api.routers.bots import router as bots_router  # RPA bot runner
from case_service.api.routers.scheduler import router as scheduler_router  # timed actions
from case_service.api.routers.permissions import router as permissions_router
from case_service.api.routers.broker import router as broker_router  # Marketplace Layer-2 data broker
from case_service.api.routers.platform_updates import router as platform_updates_router  # PUO Phase 1
from case_service.api.routers.case_variables import router as case_variables_router  # Case Variables P1
from case_service.api.routers.releases import router as releases_router, release_cron  # P66
from case_service.api.routers.hxdbmanager import router as hxdbmanager_router          # P67
from case_service.api.routers.hxdbmigrate import router as hxdbmigrate_router          # HxDBMigrate
from case_service.api.routers.hxreplay import router as hxreplay_router                # HxReplay
from case_service.api.routers.costing import router as costing_router                  # Case Costing
from case_service.api.routers.sentinel import router as sentinel_router                # HxSentinel P1
from case_service.api.routers.meet import router as meet_router                        # HxMeet P1
from case_service.api.routers.stt import router as stt_router                          # Voice input P1
from case_service.api.routers.voicefill import router as voicefill_router              # Voice input P3
from case_service.api.routers.messages import router as messages_router                # Portal v2 P4
from case_service.api.routers.hxdraft import router as hxdraft_router                  # HxDraft
from case_service.api.routers.hxevolve import router as hxevolve_router                # HxEvolve
from case_service.api.routers.hxmcp import router as hxmcp_router                      # HxNexus Operator (MCP)
from starlette.middleware.cors import CORSMiddleware

from case_service.api.health import router as health_router
from velaris_shared.config import get_settings
from velaris_shared.middleware.rate_limit import RateLimitMiddleware
from velaris_shared.middleware.request_tracking import RequestTrackingMiddleware
from velaris_shared.middleware.security_headers import SecurityHeadersMiddleware
from velaris_shared.middleware.body_limit import BodyLimitMiddleware
from velaris_shared.middleware.audit import AuditMiddleware
from velaris_shared.middleware.superadmin_gate import SuperadminGateMiddleware

logger = logging.getLogger(__name__)

# Force timestamps onto uvicorn's own access + error loggers so every log
# line in the file carries a datetime — essential for HxLogs.
_TS_FMT = logging.Formatter(
    fmt="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _uvicorn_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _uv_log = logging.getLogger(_uvicorn_logger_name)
    for _h in _uv_log.handlers:
        _h.setFormatter(_TS_FMT)
    if not _uv_log.handlers:
        _sh = logging.StreamHandler()
        _sh.setFormatter(_TS_FMT)
        _uv_log.addHandler(_sh)


_DEFAULT_JWT_SECRET = "velaris-dev-secret-change-in-production"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    settings = get_settings()

    # ── VEL-SYS-STORAGE-PRECHECK-000 — fail the storage path at BOOT ───────
    # The catalog's reference incident: `*_STORAGE_LOCAL_PATH` pointed at a
    # root-owned directory, so every upload 500'd at runtime with nothing to
    # look up. Checking writability here turns a mystery per-upload failure
    # into one loud, searchable startup line. Warn rather than abort: a
    # deployment that never uploads a document should still boot.
    if (settings.storage_backend or "local").lower() == "local":
        import os as _os
        from pathlib import Path as _Path

        _p = _Path(settings.storage_local_path)
        try:
            _p.mkdir(parents=True, exist_ok=True)
            if not _os.access(_p, _os.W_OK):
                raise PermissionError(f"{_p} is not writable by this user")
        except Exception as _exc:
            logger.warning(
                "VEL-SYS-STORAGE-PRECHECK-000: document storage path %s is not "
                "usable (%s). Every upload will fail until this is fixed — set "
                "VELARIS_CASE_STORAGE_LOCAL_PATH to a writable install-local "
                "path, or create the directory with correct ownership.",
                settings.storage_local_path, _exc,
            )

    # ── VEL-SYS-STORAGE-BUNDLED-MINIO — documents on an image nobody can re-pull
    # MinIO's images left the public registries around 2026-09-11. An install
    # whose documents are in the bundled container keeps working only for as
    # long as this host holds that image: a pruned host or a restore onto a new
    # machine cannot get it back. Say so at every start, with the way out.
    # Recognised by the container's own password being present and the endpoint
    # being on this machine — an S3 endpoint the operator supplied is not this.
    from case_service.storage.factory import bundled_minio_in_use

    if bundled_minio_in_use(settings):
        logger.warning(
            "VEL-SYS-STORAGE-BUNDLED-MINIO: documents are stored in the bundled MinIO "
            "container, whose image can no longer be downloaded. If this host loses the "
            "image, the documents cannot be served. Keep a copy of the image "
            "(docker save) or move the documents to local disk: "
            ".venv/bin/python scripts/storage-migrate.py"
        )

    # ── Security check: warn loudly if default JWT secret is still in use ───
    if settings.auth_secret == _DEFAULT_JWT_SECRET:
        logger.warning(
            "SECURITY WARNING: auth_secret is set to the default dev value. "
            "Set VELARIS_CASE_AUTH_SECRET to a long random secret before deploying to production. "
            "Example: openssl rand -hex 32"
        )

    # ── Security check: warn if HS256 shared-secret is in use instead of RS256 ──
    if not settings.auth_rsa_private_key or not settings.auth_rsa_public_key:
        logger.warning(
            "SECURITY WARNING: RSA keys are not configured — tokens are signed with "
            "HS256 (shared secret). Any service holding auth_secret can mint admin tokens. "
            "Generate a key pair and set VELARIS_CASE_AUTH_RSA_PRIVATE_KEY + "
            "VELARIS_CASE_AUTH_RSA_PUBLIC_KEY to enable RS256 before deploying to production. "
            "Run: python -c \"from velaris_shared.auth.jwt_handler import generate_rsa_keypair; generate_rsa_keypair()\""
        )
    else:
        logger.info("JWT signing: RS256 asymmetric (private key present, public key present) ✓")

    # ── Boot gate: require superadmin to exist before serving any requests ──
    try:
        from velaris_shared.db.session import get_session_factory
        from velaris_shared.db.models import HelixUserModel
        from sqlalchemy import select, func

        factory = get_session_factory()
        async with factory() as _boot_session:
            count = (await _boot_session.execute(
                select(func.count()).select_from(HelixUserModel)
                .where(HelixUserModel.is_superadmin == True)  # noqa: E712
                .where(HelixUserModel.is_active == True)       # noqa: E712
            )).scalar_one()

        if count == 0:
            logger.critical(
                "STARTUP BLOCKED: No active superadmin account found in the database. "
                "Run ./setup-velaris.sh to create one. "
                "The service will start but return 503 on all endpoints."
            )
            app.state.superadmin_missing = True
        else:
            app.state.superadmin_missing = False
            logger.info("Superadmin gate: verified (%d superadmin account)", count)
    except Exception as _boot_err:
        logger.warning("Superadmin gate check failed (DB may not be ready): %s", _boot_err)
        app.state.superadmin_missing = False  # don't block if DB isn't up yet

    # ── Seed built-in access roles ────────────────────────────────────────────
    try:
        from velaris_shared.db.session import get_session_factory
        from velaris_shared.db.models import AccessRoleModel
        from sqlalchemy import select

        _BUILT_IN_ROLES = [
            {
                "name": "developer",
                "description": "Can create and edit case types scoped to their tenant. "
                               "Admins with this role can also edit global case types.",
                "privileges": [
                    {"resource": "case_type", "actions": ["create", "read", "update", "delete"]},
                    {"resource": "form",       "actions": ["create", "read", "update", "delete"]},
                    {"resource": "workflow",   "actions": ["create", "read", "update", "delete", "execute"]},
                ],
                "tenant_id": None,  # built-in = available to all tenants
            },
        ]

        factory = get_session_factory()
        async with factory() as _seed_session:
            async with _seed_session.begin():
                for role_def in _BUILT_IN_ROLES:
                    existing = (await _seed_session.execute(
                        select(AccessRoleModel).where(
                            AccessRoleModel.name == role_def["name"],
                            AccessRoleModel.tenant_id.is_(None),
                        )
                    )).scalar_one_or_none()
                    if existing is None:
                        _seed_session.add(AccessRoleModel(**role_def))
                        logger.info("Seeded built-in role: %s", role_def["name"])
    except Exception as _seed_err:
        logger.warning("Built-in role seed failed (non-fatal): %s", _seed_err)

    # ── Phase 4 step 1: report access roles that used to grant by name ─────────
    # The merge that made an access role named `admin` a platform admin is gone.
    # Where a deployment relied on it, the loss is silent at the request path —
    # a denial for a missing privilege is indistinguishable from a correct one —
    # so it is announced here instead. Own `try`: this is a report, and a report
    # that can cancel the rest of startup is worse than no report at all.
    try:
        from velaris_shared.db.session import get_session_factory
        from velaris_shared.hxguard.role_names import warn_on_collisions

        factory = get_session_factory()
        async with factory() as _collision_session:
            await warn_on_collisions(_collision_session)
    except Exception as _collision_err:
        logger.debug("access-role name collision check skipped: %s", _collision_err)

    # ── HxVault (#19): warm per-tenant DEK cache so sync decrypt stays sync ──────
    try:
        from velaris_shared.db.session import get_session_factory
        from case_service import hxvault

        factory = get_session_factory()
        async with factory() as _vault_session:
            await hxvault.warm_cache(_vault_session)
    except Exception as _vault_err:
        # Fail LOUD: the sync decrypt path relies on the cache, so an un-warmed cache
        # means hxv2 connector-credential reads on this worker raise until the periodic
        # resync (below) repairs it. (Legacy hxv1 rows are unaffected.)
        logger.error(
            "HxVault DEK warm FAILED: %s — hxv2 connector-credential reads on this "
            "worker will fail until the periodic resync repairs the cache. hxv1 rows unaffected.",
            _vault_err,
        )

    # ── HxVault multi-worker coherence: periodic cache resync ───────────────────
    # warm_cache only runs once at startup; under multiple workers a DEK created
    # (or shredded) on another worker is invisible here until this loop reconciles.
    _vault_resync_task = None
    _vault_resync_interval = settings.vault_cache_resync_seconds
    if _vault_resync_interval and _vault_resync_interval > 0:
        import asyncio as _asyncio
        from velaris_shared.db.session import get_session_factory as _gsf_vault
        from case_service import hxvault as _hxvault_loop

        async def _vault_resync_loop():
            while True:
                await _asyncio.sleep(_vault_resync_interval)
                try:
                    async with _gsf_vault()() as _s:
                        await _hxvault_loop.resync_cache(_s)
                except _asyncio.CancelledError:
                    raise
                except Exception as _e:  # noqa: BLE001 — never let the loop die
                    logger.warning("HxVault resync loop error (will retry): %s", _e)

        _vault_resync_task = _asyncio.create_task(_vault_resync_loop())
        logger.info("HxVault DEK cache resync loop started (every %ds)", _vault_resync_interval)

    # The old start-up DDL ("Migration 042/043/044") now lives in
    # migrations/postgresql/229. Never run DDL here: `ADD COLUMN IF NOT EXISTS`
    # takes an exclusive lock before checking, and queued behind long queries it
    # held every later read of the table (R6 soak S9, 2026-11-03).

    # Start Temporal worker (embedded, for development)
    temporal_worker = None
    temporal_client = None
    try:
        from case_service.temporal.worker import (
            connect_temporal,
            start_worker,
        )

        temporal_client = await connect_temporal()
        temporal_worker = await start_worker(temporal_client)
        app.state.temporal_client = temporal_client
        # Also process-wide, for background loops that have no request and so no
        # `app.state` — a scheduled case still needs its lifecycle workflow.
        from case_service.temporal.worker import set_client as _set_temporal_client

        _set_temporal_client(temporal_client)
        logger.info("Temporal worker started (embedded)")
    except Exception as e:
        logger.warning(
            "Temporal unavailable: %s — case lifecycle "
            "workflows will not auto-execute",
            e,
        )
        app.state.temporal_client = None

    # >>> P32 realtime Redis bridge
    redis_bridge = None
    try:
        from velaris_shared.config import get_settings as _gs
        from velaris_shared.redis_client import get_redis
        from case_service.realtime.manager import get_manager
        from case_service.realtime.redis_bridge import RedisBridge
        _s = _gs()
        if getattr(_s, "redis_enabled", False):
            _r = await get_redis()
            if _r is not None:
                import os as _os, uuid as _uuid
                instance_id = _os.environ.get("HOSTNAME", str(_uuid.uuid4())[:8])
                redis_bridge = RedisBridge(get_manager(), _r, prefix=_s.redis_channel_prefix, instance_id=instance_id)
                await redis_bridge.start()
                get_manager().attach_redis_bridge(redis_bridge)
                logger.info("Redis realtime bridge attached (instance=%s)", instance_id)
    except Exception as _e:
        logger.warning("Redis realtime bridge unavailable: %s", _e)
    # <<< P32 realtime Redis bridge

    # >>> R5e step 9: the background jobs — case_service/jobs.py. Each singleton
    # runs under a job lease, so however many processes start them (workers,
    # replicas, the worker service) one runs each. VELARIS_CASE_RUN_JOBS=false
    # leaves them to the worker service; unset = here, exactly as before.
    _jobs = None
    if get_settings().run_jobs:
        try:
            from case_service.jobs import start_jobs
            _jobs = await start_jobs()
        except Exception as _e:
            logger.error("Background jobs unavailable: %s", _e)
    else:
        logger.info("Background jobs: off here (VELARIS_CASE_RUN_JOBS=false) — the worker service runs them")
    # <<< R5e step 9

    # >>> Background schedulers
    # One try/except EACH, deliberately. These tasks are unrelated, and grouping
    # them behind a single import meant a failure in the first silently cancelled
    # the rest: an ImportError in credential_monitor never scheduled release_cron,
    # which left `_ENABLED_VERSIONS` empty, which makes is_feature_enabled() return
    # False for EVERY feature platform-wide — reported only as "Credential expiry
    # monitor unavailable". Each scheduler now fails alone.
    import asyncio as _asyncio

    try:
        _asyncio.create_task(release_cron())
        logger.info("Release/feature-flag cron scheduled")
    except Exception as _e:
        # Loud: without this cron every feature flag reads as off.
        logger.error("Release cron unavailable — ALL feature flags will read as "
                     "disabled: %s", _e)

    # <<< Background schedulers

    # >>> Phase 6: role → privilege store cache
    # Own try/except, same reason as above. If this never starts, the cache stays
    # empty and every role falls back to its seeded privileges — i.e. exactly the
    # pre-Phase-6 behaviour, which is why the store is additive rather than a
    # replacement. Nobody is locked out by a cron that failed to schedule.
    try:
        from velaris_shared.hxguard.role_store import role_privileges_cron
        _asyncio.create_task(role_privileges_cron())
        logger.info("Role privilege store refresh scheduled")
    except Exception as _e:
        logger.warning("Role privilege store unavailable, seeded privileges only: %s", _e)
    # <<< Phase 6 role privilege store

    # >>> Phase C (#16-C): the shared authorization generation.
    # case-service is where the mutations happen, so it is the one process that
    # never needed this to see its own changes. It needs it to see the OTHER
    # services' — HxGraph writes tuples on sync, and a bot or connector deleted
    # there is an authz input change here.
    try:
        from velaris_shared.hxguard.generation import generation_cron
        _asyncio.create_task(generation_cron())
        logger.info("Authz generation poll scheduled")
    except Exception as _e:
        logger.warning("Authz generation poll unavailable: %s", _e)
    # <<< Phase C

    # >>> R5e step 5: HxStream traces from the other services (and other copies
    # of this one) reach the /hxstream/ws subscribers here. Cache on only.
    try:
        if get_settings().redis_enabled:
            from velaris_shared.hxstream.emitter import relay_from_other_processes
            _asyncio.create_task(relay_from_other_processes())
            logger.info("HxStream cross-process relay started")
    except Exception as _e:
        logger.warning("HxStream cross-process relay unavailable: %s", _e)

    # >>> R5e step 5: the same signal for every other shared cache (feature
    # flags, revoked sessions, data keys, tenant lookups — velaris_shared/generations.py).
    try:
        from velaris_shared.generations import generations_cron
        _asyncio.create_task(generations_cron())
        logger.info("Platform generations poll scheduled")
    except Exception as _e:
        logger.warning("Platform generations poll unavailable: %s", _e)
    # <<< R5e step 5

    # >>> HxShield detection (roadmap #25)
    # The engine's eight patterns had never run against real traffic: it had one
    # caller, the explicit /hxshield/score endpoint. Registering here wires it to
    # `log_security_event`, the chokepoint 51 sites already use. The registration
    # is unconditional and the FLAG is checked per event, so detection can be
    # switched on without a restart — and stays off by default.
    try:
        from case_service.hxshield.detector import install as _install_shield
        _install_shield()
        logger.info(
            "HxShield detector registered (scoring %s)",
            "ON" if get_settings().hxshield_detect_enabled else "OFF — set "
            "VELARIS_CASE_HXSHIELD_DETECT_ENABLED=true to enable",
        )
    except Exception as _e:
        logger.warning("HxShield detector unavailable: %s", _e)
    # <<< HxShield

    yield

    # >>> HxVault resync loop stop
    if _vault_resync_task is not None:
        _vault_resync_task.cancel()
        try:
            await _vault_resync_task
        except (Exception, BaseException):
            pass
    # <<< HxVault resync loop stop

    # >>> R5e step 9: background jobs stop (releases their leases)
    if _jobs is not None:
        await _jobs.stop()
    # <<< R5e step 9

    # Shutdown
    if temporal_worker is not None:
        from case_service.temporal.worker import stop_worker

        await stop_worker(temporal_worker)
    if redis_bridge is not None:
        try:
            await redis_bridge.stop()
        except Exception:
            pass
    logger.info("Case service shutting down")


def create_app() -> FastAPI:
    settings = get_settings()
    from fastapi import Depends as _Depends

    from case_service.core.tenant_path_guard import tenant_path_guard

    app = FastAPI(
        title="Velaris Case Service",
        description="Case management API for the Velaris BPM platform",
        version="0.1.0",
        lifespan=lifespan,
        # 8d (2026-09-29): every route keyed by a tenant-owned id refuses
        # another tenant's row, in one place. See core/tenant_path_guard.py.
        dependencies=[_Depends(tenant_path_guard)],
    )

    # Commit each request's database work BEFORE its response is sent. Must be
    # set before any include_router below: FastAPI captures the route class at
    # registration time. See velaris_shared/db/commit_route.py.
    install_commit_before_response(app)

    # Error catalog wiring: attach the stable code to the response body and
    # the log line, so the identifier in `docs/Future/error-catalog.md` is the
    # same string a support engineer sees in both places. Uncoded
    # HTTPExceptions are re-raised untouched — this only enriches, never
    # reshapes an existing contract.
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    from velaris_shared.core.error_codes import (
        DB_UNAVAILABLE_503,
        SYS_UNHANDLED_500,
        VAULT_DEK_503,
        VAULT_SHREDDED_410,
        CodedHTTPException,
        error_payload,
    )
    from sqlalchemy.exc import InterfaceError, OperationalError

    from case_service.hxvault import DekNotLoadedError
    from case_service.hxvault.keyring import KeyShreddedError

    # VEL-DOC-UUID-422: FastAPI rejects a malformed path UUID before any
    # handler runs, so the code cannot be raised from a route — it has to be
    # attached to the framework's own validation failure. Scoped to UUID path
    # params on document routes so every other 422 keeps its default body.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request, exc: RequestValidationError):  # noqa: ANN001
        from velaris_shared.core.error_codes import DOC_UUID_422, error_payload

        errors = exc.errors()
        uuid_path_error = any(
            e.get("type", "").startswith("uuid")
            and "path" in (e.get("loc") or ())
            for e in errors
        )
        if uuid_path_error and "/documents" in request.url.path:
            logging.getLogger("case_service.errors").warning(
                "%s %s -> 422 %s", request.method, request.url.path, DOC_UUID_422,
            )
            return JSONResponse(
                status_code=422,
                content=error_payload(DOC_UUID_422, jsonable_encoder(errors)),
            )
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})

    @app.exception_handler(CodedHTTPException)
    async def _coded_error_handler(request, exc: CodedHTTPException):  # noqa: ANN001
        logging.getLogger("case_service.errors").warning(
            "%s %s -> %s %s", request.method, request.url.path,
            exc.status_code, exc.code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.detail),
            headers=getattr(exc, "headers", None),
        )

    # SCIM's own error shape is user-service's now (R5e step 10).

    # HxVault and database faults reached the client as bare 500s: both are
    # raised deep in library code (crypto, the driver) and no router catches
    # them. A 500 tells an operator nothing about which of these happened, and
    # the three differ completely in what to do next — so they are mapped here,
    # at the one place every request passes through.
    # A legal hold refuses from deep inside retention, erasure and the vault —
    # places no router catches. Mapped here so every one of them answers the
    # same 409 with the same code instead of a bare 500.
    from case_service.legal_hold.service import LegalHoldError

    @app.exception_handler(LegalHoldError)
    async def _legal_hold_handler(request, exc):  # noqa: ANN001
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, str(exc)),
        )

    @app.exception_handler(KeyShreddedError)
    async def _vault_shredded_handler(request, exc):  # noqa: ANN001
        # 410 Gone, not 500 or 404: the object existed, the key was destroyed
        # on purpose, and it is NOT coming back. Anything retryable-looking
        # invites a retry loop and then a pointless restore attempt.
        logging.getLogger("case_service.errors").error(
            "%s %s -> shredded DEK: %s", request.method, request.url.path, exc,
        )
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
        # 503, deliberately different from the above: this one IS transient —
        # a sealed or unreachable OpenBao, or a missing KEK. Retrying is
        # reasonable once an operator has fixed the vault.
        logging.getLogger("case_service.errors").error(
            "%s %s -> DEK unavailable: %s", request.method, request.url.path, exc,
        )
        return JSONResponse(
            status_code=503,
            content=error_payload(
                VAULT_DEK_503,
                "Encryption keys are unavailable. The vault may be sealed or "
                "unreachable; the data itself is intact.",
            ),
        )

    async def _db_unavailable_handler(request, exc):  # noqa: ANN001
        # The driver's message carries host, port, user and sometimes the
        # database name. It goes to the log, never to the client.
        logging.getLogger("case_service.errors").error(
            "%s %s -> database unavailable: %s", request.method, request.url.path, exc,
        )
        return JSONResponse(
            status_code=503,
            content=error_payload(
                DB_UNAVAILABLE_503,
                "The database is unavailable. This is not a problem with your request.",
            ),
        )

    # Registered for a TUPLE, because `OperationalError` alone missed the most
    # common way a database goes away.
    #
    # SQLAlchemy only wraps a fault into `OperationalError` once a connection
    # exists. When the server is simply not there, asyncpg's connect fails
    # first and a bare `ConnectionRefusedError` propagates untouched — so on
    # 2026-08-05 a stopped Postgres produced Starlette's default `text/plain`
    # 500 instead of this handler's clean 503, while the handler that describes
    # exactly that situation sat one exception type away.
    #
    # `ConnectionError` (the builtin) is safe to claim for the database here:
    # httpx raises its own `httpx.ConnectError`, which does NOT inherit from it,
    # so outbound connector and LLM failures cannot be mislabelled as a database
    # fault. `InterfaceError` covers a connection dropped mid-statement.
    _DB_FAULTS = (OperationalError, InterfaceError, ConnectionError)
    for _fault in _DB_FAULTS:
        app.add_exception_handler(_fault, _db_unavailable_handler)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request, exc):  # noqa: ANN001
        # The floor under every handler above. Starlette's built-in fallback
        # returns `text/plain` "Internal Server Error", which every client in
        # this platform then fails to parse — the browser reports a JSON syntax
        # error and the real fault stays invisible. This keeps the contract
        # "every response from this API is JSON" true even for bugs.
        #
        # `exc_info=True` because this is, by definition, the case nobody
        # classified: without the traceback in the log there is nothing left to
        # debug from. The client gets none of it — no message, no type, no
        # stack, since an unclassified error is exactly the kind most likely to
        # carry a connection string or a token in its text.
        logging.getLogger("case_service.errors").error(
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

    # A lost connection that a route's `except Exception` swallowed surfaces on
    # the next query as PendingRollbackError (R6 failure test 2026-11-03:
    # PostgreSQL killed mid-create answered 500). Nothing was committed, so it is
    # "database unavailable". Any other pending rollback is a real bug: 500.
    from sqlalchemy.exc import PendingRollbackError as _PendingRollback

    @app.exception_handler(_PendingRollback)
    async def _pending_rollback_handler(request, exc):  # noqa: ANN001
        if "Can't reconnect until invalid transaction is rolled back" in str(exc):
            return await _db_unavailable_handler(request, exc)
        return await _unhandled_handler(request, exc)

    # A connection dropped mid-query (pooler or database killed under load)
    # can arrive as a plain DBAPIError, e.g. asyncpg ConnectionDoesNotExistError
    # (R6 failure test 2026-11-03: GET /cases answered 500). SQLAlchemy marks it
    # `connection_invalidated` → 503. Any other DBAPIError stays a 500.
    from sqlalchemy.exc import DBAPIError as _DBAPIError

    @app.exception_handler(_DBAPIError)
    async def _dbapi_handler(request, exc):  # noqa: ANN001
        if getattr(exc, "connection_invalidated", False):
            return await _db_unavailable_handler(request, exc)
        return await _unhandled_handler(request, exc)

    # >>> P23 observability wiring
    configure_logging()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(observability_router)
    try:
        configure_telemetry('case-service', app=app)
    except Exception:
        pass
    # <<< P23 observability wiring


    prefix = "/api/v1"
    app.include_router(case_types.router, prefix=prefix)
    app.include_router(cases.router, prefix=prefix)
    app.include_router(case_config.router, prefix=prefix)
    app.include_router(case_tenant_transfers.router, prefix=prefix)
    app.include_router(legal_holds.router, prefix=prefix)
    app.include_router(assignments.router, prefix=prefix)
    app.include_router(queues.router, prefix=prefix)
    app.include_router(sla.router, prefix=prefix)
    app.include_router(my_work.router, prefix=prefix)
    app.include_router(rules.router, prefix=prefix)
    app.include_router(forms.router, prefix=prefix)
    from case_service.api.routers import reference_tables as _reference_tables
    app.include_router(_reference_tables.router, prefix=prefix)   # Form Builder v2 step 3
    from case_service.api.routers import csp_reports as _csp_reports
    app.include_router(_csp_reports.router, prefix=prefix)        # Studio CSP reports (anonymous, capped)
    from case_service.api.routers import form_sections as _form_sections
    app.include_router(_form_sections.router, prefix=prefix)      # Form Builder §5 step 6
    app.include_router(data_models.router, prefix=prefix)
    app.include_router(form_submissions_router, prefix=prefix)
    app.include_router(webhooks_router, prefix=prefix)
    app.include_router(admin_router, prefix=prefix)
    # /auth, /auth/real, /auth/saml, /scim, /identity/provisioning and
    # /user-directory moved to user-service (R5e step 10). POST
    # /auth/switch-context (access_groups.py) stays here; the gateway sends
    # that one path back.
    # /analytics and /process-mining are served by analytics-service since 13c.
    # The gateway routes both prefixes there; nothing is mounted here, so a
    # direct hit on case-service 404s rather than silently answering with a
    # second copy of the same endpoints.
    app.include_router(nlp_router, prefix=prefix)
    app.include_router(tenants_router, prefix=prefix)
    app.include_router(codegen_router, prefix=prefix)
    app.include_router(enterprise_router, prefix=prefix)
    app.include_router(sitemap_router, prefix=prefix)
    app.include_router(realtime_router, prefix=prefix)
    app.include_router(documents_router, prefix=prefix)
    app.include_router(escalation_router, prefix=prefix)
    app.include_router(compliance_router, prefix=prefix)
    app.include_router(email_router, prefix=prefix)
    app.include_router(push_router, prefix=prefix)
    app.include_router(hxnexus_router, prefix=prefix)
    app.include_router(releases_router, prefix=prefix)
    app.include_router(hxdbmanager_router, prefix=prefix)
    app.include_router(access_groups_router, prefix=prefix)
    app.include_router(hxstream_router, prefix=prefix)
    app.include_router(status_router, prefix=prefix)
    app.include_router(knowledge_router, prefix=prefix)
    # /graph is served by hxgraph-service since 13d. The gateway routes the
    # prefix there; nothing is mounted here.

    # /hxmigrate, /importer, /scout, /scout-ai and /orchestrator are served by
    # hxmigrate-service since 13e — the migration domain left as one unit
    # because its four packages import each other.
    app.include_router(apps_router, prefix=prefix)
    app.include_router(hxbridge_router, prefix=prefix)
    app.include_router(webhook_router, prefix=prefix)
    app.include_router(hxsync_router, prefix=prefix)
    app.include_router(audit_stream_router, prefix=prefix)
    app.include_router(hxglobal_router, prefix=prefix)
    app.include_router(hxshield_router, prefix=prefix)
    app.include_router(hxfusion_router, prefix=prefix)
    app.include_router(identity_router, prefix=prefix)   # P49
    app.include_router(esign_router,    prefix=prefix)   # P49
    app.include_router(crm_router,      prefix=prefix)   # P50
    app.include_router(invoice_router,  prefix=prefix)   # P50
    app.include_router(comms_router,    prefix=prefix)   # P51
    app.include_router(docintel_router, prefix=prefix)   # P52
    app.include_router(devconn_router,   prefix=prefix)   # P53
    app.include_router(calendar_router, prefix=prefix)  # Calendar P1
    app.include_router(arbiter_router, prefix=prefix)   # HxArbiter ARB-A
    # F-05 first: `/agents/workforce` must be matched before `/agents/{agent_id}`,
    # which would otherwise capture it and 422 on the UUID parse.
    app.include_router(agents_workforce_router, prefix=prefix)  # F-05 roster
    app.include_router(agents_router, prefix=prefix)    # F-00 agent identity
    app.include_router(capture_router, prefix=prefix)   # F-06 ambient capture
    app.include_router(ai_ledger_router, prefix=prefix) # F-01 AI decision ledger
    app.include_router(healing_router, prefix=prefix)       # F-08 self-healing processes
    app.include_router(handoff_router, prefix=prefix)       # F-09 agent handoff
    app.include_router(field_router, prefix=prefix)         # F-10 field operations
    app.include_router(rollouts_router, prefix=prefix)      # Oct 18a staged rollouts
    app.include_router(intent_router, prefix=prefix)        # F-11 intent execution
    app.include_router(negotiate_router, prefix=prefix)     # F-12 negotiation
    app.include_router(engage_router, prefix=prefix)        # item 12 rep desk
    app.include_router(proofs_router, prefix=prefix)    # F-03 compliance proofs
    app.include_router(hxdeploy_router,  prefix=prefix)   # P55
    app.include_router(hxdbmigrate_router, prefix=prefix)  # HxDBMigrate
    app.include_router(hxreplay_router, prefix=prefix)      # HxReplay
    app.include_router(costing_router, prefix=prefix)        # Case Costing
    app.include_router(sentinel_router, prefix=prefix)       # HxSentinel P1
    app.include_router(meet_router, prefix=prefix)           # HxMeet P1
    app.include_router(stt_router, prefix=prefix)             # Voice input P1 (local STT)
    app.include_router(voicefill_router, prefix=prefix)       # Voice input P3 (form-fill)
    # /portal and /portal-admin moved to portal-service (R5e step 8).
    app.include_router(messages_router, prefix=prefix)       # Portal v2 P4 case messages
    app.include_router(hxdraft_router, prefix=prefix)         # HxDraft
    app.include_router(hxevolve_router, prefix=prefix)        # HxEvolve
    app.include_router(hxmcp_router, prefix=prefix)            # HxNexus Operator (MCP)
    app.include_router(hxwork_router,    prefix=prefix)   # P56
    app.include_router(hxcanvas_router,  prefix=prefix)   # P57
    app.include_router(hxdocs_router,    prefix=prefix)   # P58
    app.include_router(branches_router,  prefix=prefix)   # P60 HxBranch
    app.include_router(commits_router,   prefix=prefix)   # P61 Commit pattern
    app.include_router(intake_router,    prefix=prefix)   # Process-Case integration
    app.include_router(hxlogs_router,   prefix=prefix)   # P63 HxLogs
    app.include_router(channels_router, prefix=prefix)        # Omnichannel inbound
    app.include_router(bots_router, prefix=prefix)            # RPA bot runner
    app.include_router(scheduler_router, prefix=prefix)       # timed actions
    app.include_router(permissions_router, prefix=prefix)
    app.include_router(broker_router, prefix=prefix)  # Layer-2 scoped data broker (container-only surface)
    app.include_router(platform_updates_router, prefix=prefix)  # PUO Phase 1
    app.include_router(case_variables_router, prefix=prefix)  # Case Variables P1
    from case_service.api.routers import testsuite as _testsuite
    app.include_router(_testsuite.router, prefix=prefix)  # Test Suite core (#27)
    from case_service.api.routers import hxtest as _hxtest
    app.include_router(_hxtest.router, prefix=prefix)  # HxTest marketplace AI layer (#27)


    # Middleware (order matters — last added = first executed)
    app.add_middleware(SuperadminGateMiddleware)  # boot gate — must be outermost
    app.add_middleware(AuditMiddleware)            # system-wide action audit
    # R5d 11b: read-your-own-writes mark + where an opted-in read ran.
    from velaris_shared.middleware.read_routing import ReadRoutingMiddleware
    app.add_middleware(ReadRoutingMiddleware)
    app.add_middleware(RequestTrackingMiddleware)
    import os
    if not settings.debug and "PYTEST_CURRENT_TEST" not in os.environ:
        # >>> P32 scaling: backend selector
        # Per client address, matched to the gateway — see velaris_shared/app.py (R6, 2026-10-30).
        if getattr(settings, "rate_limit_backend", "memory") == "redis" and getattr(settings, "redis_enabled", False):
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
        # <<< P32 scaling
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.debug else [],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # >>> Group D: oversized bodies rejected before inner middleware/endpoints
    # do work; SecurityHeaders outermost so headers land on every response,
    # including 413/429 rejections
    app.add_middleware(
        BodyLimitMiddleware,
        max_body_bytes=settings.max_body_bytes,
        max_upload_bytes=settings.max_upload_bytes,
        max_migrate_upload_bytes=settings.max_migrate_upload_bytes,
    )
    # >>> Workload identity (#18): the gateway is the only permitted CALLER,
    # not merely the only route. Second to last so it runs second — refusing
    # before AuditMiddleware opens a session for a caller that has no business
    # being here. Inert until VELARIS_GATEWAY_IDENTITY_SECRET is set.
    from velaris_shared.middleware.gateway_identity import GatewayIdentityMiddleware
    app.add_middleware(
        GatewayIdentityMiddleware,
        secret=settings.gateway_identity_secret,
        service_name="case-service",
    )
    # <<<
    app.add_middleware(SecurityHeadersMiddleware)
    # <<< Group D

    # Health endpoints (no prefix — /health and /ready at root)
    app.include_router(health_router)
    # R5e step 7 (the commerce split): /api/v1/{checkout,payments,storefront,
    # marketplace,model-market} are served by commerce-service now, behind the
    # same gateway URLs. What it asks case-service to do arrives here. Not under
    # /api/v1 — the gateway never forwards /internal; the routes refuse any call
    # without the install's internal service secret.
    from case_service.internal_api.router import router as _internal_router
    app.include_router(_internal_router)

    return app


app = create_app()