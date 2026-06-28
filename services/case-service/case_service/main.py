"""HELIX Case Service — FastAPI application.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from case_service.api.routers import (
    assignments,
    case_types,
    cases,
    data_models,
    forms,
    my_work,
    queues,
    rules,
    sla,
)

from case_service.observability import (
    ObservabilityMiddleware, configure_logging, configure_telemetry,
)
from case_service.api.observability import router as observability_router
from case_service.api.routers.form_submissions import router as form_submissions_router
from case_service.api.routers.analytics import router as analytics_router
from case_service.api.routers.webhooks import router as webhooks_router
from case_service.api.routers.admin import router as admin_router
from case_service.api.routers.auth import router as auth_router
from case_service.api.routers.process_mining import router as process_mining_router
from case_service.api.routers.nlp import router as nlp_router
from case_service.api.routers.scout import router as scout_router
from case_service.api.routers.tenants import router as tenants_router
from case_service.api.routers.codegen import router as codegen_router
from case_service.api.routers.scout_ai import router as scout_ai_router
from case_service.api.routers.enterprise import router as enterprise_router
from case_service.api.routers.sitemap import router as sitemap_router
from case_service.api.routers.orchestrator import router as orchestrator_router
from case_service.api.routers.realtime import router as realtime_router
from case_service.api.routers.documents import router as documents_router
from case_service.api.routers.escalation import router as escalation_router
from case_service.api.routers.user_directory import router as user_directory_router
from case_service.api.routers.compliance import router as compliance_router
from case_service.api.routers.email import router as email_router
from case_service.api.routers.push import router as push_router
from case_service.api.routers.hxnexus import router as hxnexus_router
from case_service.api.routers.portal import public_router as portal_router, admin_router as portal_admin_router
from case_service.api.routers.access_groups import router as access_groups_router
from case_service.api.routers.hxstream import router as hxstream_router
from case_service.api.routers.knowledge import router as knowledge_router
from case_service.api.routers.graph import router as graph_router
from case_service.api.routers.apps import router as apps_router
from case_service.api.routers.importer import router as importer_router
from case_service.api.routers.hxbridge import router as hxbridge_router, webhook_router
from case_service.api.routers.hxsync import router as hxsync_router
from case_service.api.routers.hxglobal import router as hxglobal_router
from case_service.api.routers.hxshield import router as hxshield_router
from case_service.api.routers.hxfusion import router as hxfusion_router
from case_service.api.routers.payments import router as payments_router
from case_service.api.routers.checkout import router as checkout_router  # HxCheckout (marketplace)
from case_service.api.routers.storefront import router as storefront_router  # HxStorefront (marketplace)
from case_service.api.routers.storefront_public import router as storefront_public_router  # HxStorefront public
from case_service.api.routers.kyc import identity_router, esign_router
from case_service.api.routers.crm import crm_router, invoice_router
from case_service.api.routers.comms import router as comms_router
from case_service.api.routers.docintel import router as docintel_router
from case_service.api.routers.devconn import router as devconn_router
from case_service.api.routers.hxmigrate import router as hxmigrate_router
from case_service.api.routers.hxdeploy import router as hxdeploy_router
from case_service.api.routers.hxwork import router as hxwork_router
from case_service.api.routers.hxcanvas import router as hxcanvas_router
from case_service.api.routers.hxdocs import router as hxdocs_router
from case_service.api.routers.branches import router as branches_router   # P60 HxBranch
from case_service.api.routers.intake   import router as intake_router     # Process-Case integration
from case_service.api.routers.commits   import router as commits_router    # P61 Commit pattern
from case_service.api.routers.hxlogs   import router as hxlogs_router     # P63 HxLogs
from case_service.api.routers.auth_real import router as auth_real_router  # P64 Real Auth
from case_service.api.routers.permissions import router as permissions_router
from case_service.api.routers.marketplace import router as marketplace_router  # Marketplace
from case_service.api.routers.platform_updates import router as platform_updates_router  # PUO Phase 1
from case_service.api.routers.case_variables import router as case_variables_router  # Case Variables P1
from case_service.api.routers.portal_customers import public_router as portal_customers_public_router, admin_router as portal_customers_admin_router  # P65
from case_service.api.routers.releases import router as releases_router, release_cron  # P66
from case_service.api.routers.hxdbmanager import router as hxdbmanager_router          # P67
from starlette.middleware.cors import CORSMiddleware

from case_service.api.health import router as health_router
from case_service.config import get_settings
from case_service.middleware.rate_limit import RateLimitMiddleware
from case_service.middleware.request_tracking import RequestTrackingMiddleware
from case_service.middleware.security_headers import SecurityHeadersMiddleware
from case_service.middleware.body_limit import BodyLimitMiddleware
from case_service.middleware.audit import AuditMiddleware
from case_service.middleware.superadmin_gate import SuperadminGateMiddleware

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


_DEFAULT_JWT_SECRET = "helix-dev-secret-change-in-production"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    settings = get_settings()

    # ── Security check: warn loudly if default JWT secret is still in use ───
    if settings.auth_secret == _DEFAULT_JWT_SECRET:
        logger.warning(
            "SECURITY WARNING: auth_secret is set to the default dev value. "
            "Set HELIX_CASE_AUTH_SECRET to a long random secret before deploying to production. "
            "Example: openssl rand -hex 32"
        )

    # ── Security check: warn if HS256 shared-secret is in use instead of RS256 ──
    if not settings.auth_rsa_private_key or not settings.auth_rsa_public_key:
        logger.warning(
            "SECURITY WARNING: RSA keys are not configured — tokens are signed with "
            "HS256 (shared secret). Any service holding auth_secret can mint admin tokens. "
            "Generate a key pair and set HELIX_CASE_AUTH_RSA_PRIVATE_KEY + "
            "HELIX_CASE_AUTH_RSA_PUBLIC_KEY to enable RS256 before deploying to production. "
            "Run: python -c \"from case_service.auth.jwt_handler import generate_rsa_keypair; generate_rsa_keypair()\""
        )
    else:
        logger.info("JWT signing: RS256 asymmetric (private key present, public key present) ✓")

    # ── Boot gate: require superadmin to exist before serving any requests ──
    try:
        from case_service.db.session import get_session_factory
        from case_service.db.models import HelixUserModel
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
        from case_service.db.session import get_session_factory
        from case_service.db.models import AccessRoleModel
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

    # ── HxVault (#19): warm per-tenant DEK cache so sync decrypt stays sync ──────
    try:
        from case_service.db.session import get_session_factory
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
        from case_service.db.session import get_session_factory as _gsf_vault
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

    # ── Migration 042: add v2 ownership columns to artifact_branches ─────────
    try:
        from case_service.db.session import get_engine
        from sqlalchemy import text as _sql_text

        _DDL_042 = [
            "ALTER TABLE artifact_branches ADD COLUMN IF NOT EXISTS owner_id TEXT",
            "ALTER TABLE artifact_branches ADD COLUMN IF NOT EXISTS assigned_reviewer_id TEXT",
            "ALTER TABLE artifact_branches ADD COLUMN IF NOT EXISTS access_group_id UUID",
        ]
        _eng = get_engine()
        async with _eng.begin() as _conn:
            for _stmt in _DDL_042:
                await _conn.execute(_sql_text(_stmt))
        logger.info("Migration 042 applied (artifact_branches v2 columns)")
    except Exception as _m042_err:
        logger.warning("Migration 042 non-fatal: %s", _m042_err)
    # ── End migration 042 ────────────────────────────────────────────────────

    # ── Migration 043: create branch_audit_events table ─────────────────────────
    try:
        from case_service.db.session import get_engine
        from sqlalchemy import text as _sql_text

        _DDL_043 = """
        CREATE TABLE IF NOT EXISTS branch_audit_events (
            id UUID PRIMARY KEY,
            branch_id UUID NOT NULL,
            event_type VARCHAR(60) NOT NULL,
            actor_id TEXT,
            actor_name TEXT,
            metadata JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
        _IDX_043 = [
            "CREATE INDEX IF NOT EXISTS ix_bae_branch  ON branch_audit_events (branch_id)",
            "CREATE INDEX IF NOT EXISTS ix_bae_created ON branch_audit_events (created_at)",
        ]
        _eng = get_engine()
        async with _eng.begin() as _conn:
            await _conn.execute(_sql_text(_DDL_043))
            for _idx in _IDX_043:
                await _conn.execute(_sql_text(_idx))
        logger.info("Migration 043 applied (branch_audit_events table)")
    except Exception as _m043_err:
        logger.warning("Migration 043 non-fatal: %s", _m043_err)
    # ── End migration 043 ────────────────────────────────────────────────────

    # ── Migration 044: intake trigger fields on case_types + intake_events table ─
    try:
        from case_service.db.session import get_engine
        from sqlalchemy import text as _sql_text

        _DDL_044_CT = [
            "ALTER TABLE case_types ADD COLUMN IF NOT EXISTS intake_trigger VARCHAR(20) NOT NULL DEFAULT 'manual'",
            "ALTER TABLE case_types ADD COLUMN IF NOT EXISTS trigger_connector_id UUID",
            "ALTER TABLE case_types ADD COLUMN IF NOT EXISTS filter_conditions JSONB NOT NULL DEFAULT '{}'",
            "ALTER TABLE case_types ADD COLUMN IF NOT EXISTS field_mapping JSONB NOT NULL DEFAULT '{}'",
            "ALTER TABLE case_types ADD COLUMN IF NOT EXISTS process_definition_id UUID",
        ]
        _DDL_044_IE = """
        CREATE TABLE IF NOT EXISTS intake_events (
            id UUID PRIMARY KEY,
            case_type_id UUID REFERENCES case_types(id) ON DELETE SET NULL,
            connector_id UUID,
            source_ip VARCHAR(50),
            raw_payload JSONB NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'received',
            filter_result JSONB NOT NULL DEFAULT '{}',
            created_case_id UUID,
            process_instance_id UUID,
            error TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processed_at TIMESTAMPTZ
        )
        """
        _DDL_044_IDX = [
            "CREATE INDEX IF NOT EXISTS ix_intake_case_type ON intake_events (case_type_id)",
            "CREATE INDEX IF NOT EXISTS ix_intake_status    ON intake_events (status)",
            "CREATE INDEX IF NOT EXISTS ix_intake_received  ON intake_events (received_at)",
            "CREATE INDEX IF NOT EXISTS ix_intake_case      ON intake_events (created_case_id)",
        ]
        _eng = get_engine()
        async with _eng.begin() as _conn:
            for _stmt in _DDL_044_CT:
                await _conn.execute(_sql_text(_stmt))
            await _conn.execute(_sql_text(_DDL_044_IE))
            for _idx in _DDL_044_IDX:
                await _conn.execute(_sql_text(_idx))
        logger.info("Migration 044 applied (intake trigger fields + intake_events)")
    except Exception as _m044_err:
        logger.warning("Migration 044 non-fatal: %s", _m044_err)
    # ── End migration 044 ────────────────────────────────────────────────────

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
        from case_service.config import get_settings as _gs
        from case_service.redis_client import get_redis
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

    # >>> P25 email poll loop
    email_loop = None
    try:
        from case_service.mail.worker import get_poll_loop
        email_loop = get_poll_loop()
        await email_loop.start()
    except Exception as _e:
        import logging; logging.getLogger(__name__).warning("Email poll loop unavailable: %s", _e)
    # <<< P25 email poll loop

    # >>> SD-6 credential expiry monitor
    import asyncio as _asyncio
    try:
        from case_service.security.credential_monitor import credential_expiry_monitor
        _asyncio.create_task(credential_expiry_monitor())
        _asyncio.create_task(release_cron())
        logger.info("Credential expiry monitor started")
    except Exception as _e:
        logger.warning("Credential expiry monitor unavailable: %s", _e)
    # <<< SD-6 credential expiry monitor

    # >>> Group I: daily RFC-3161 anchoring of the audit chain
    try:
        if get_settings().audit_anchor_enabled:
            from case_service.compliance.audit_anchor import audit_anchor_loop
            _asyncio.create_task(audit_anchor_loop())
            logger.info("Audit anchor loop started (TSA: %s)", get_settings().audit_tsa_url)
    except Exception as _e:
        logger.warning("Audit anchor loop unavailable: %s", _e)
    # <<< Group I audit anchoring

    # >>> C1 outbox relay
    _outbox_relay = None
    try:
        from case_service.integrations.outbox_relay import OutboxRelay
        from case_service.db.session import get_session_factory as _gsf
        _outbox_relay = OutboxRelay(_gsf())
        _outbox_relay.start()
    except Exception as _e:
        logger.warning("OutboxRelay unavailable: %s", _e)
    # <<< C1 outbox relay

    # >>> PUO Phase 2: platform update watcher (admin notifications)
    _puo_watcher = None
    try:
        from case_service.integrations.platform_update_watcher import PlatformUpdateWatcher
        from case_service.db.session import get_analytics_session_factory as _gasf
        _puo_watcher = PlatformUpdateWatcher(_gasf())
        _puo_watcher.start()
    except Exception as _e:
        logger.warning("PlatformUpdateWatcher unavailable: %s", _e)
    # <<< PUO Phase 2

    # >>> PUO Phase 3: rollout plan engine (rings, soak, supersede)
    _puo_engine = None
    try:
        from case_service.integrations.platform_update_plan_engine import PlatformUpdatePlanEngine
        from case_service.db.session import get_analytics_session_factory as _gasf2
        _puo_engine = PlatformUpdatePlanEngine(_gasf2())
        _puo_engine.start()
    except Exception as _e:
        logger.warning("PlatformUpdatePlanEngine unavailable: %s", _e)
    # <<< PUO Phase 3

    yield

    # >>> HxVault resync loop stop
    if _vault_resync_task is not None:
        _vault_resync_task.cancel()
        try:
            await _vault_resync_task
        except (Exception, BaseException):
            pass
    # <<< HxVault resync loop stop

    # >>> C1 outbox relay stop
    if _outbox_relay is not None:
        _outbox_relay.stop()
    # <<< C1 outbox relay stop

    # >>> PUO Phase 2 stop
    if _puo_watcher is not None:
        _puo_watcher.stop()
    # <<< PUO Phase 2 stop

    # >>> PUO Phase 3 stop
    if _puo_engine is not None:
        _puo_engine.stop()
    # <<< PUO Phase 3 stop

    # >>> P25 email poll loop stop
    if email_loop is not None:
        try:
            await email_loop.stop()
        except Exception:
            pass
    # <<< P25 email poll loop stop

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
    app = FastAPI(
        title="Velaris Case Service",
        description="Case management API for the Velaris BPM platform",
        version="0.1.0",
        lifespan=lifespan,
    )

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
    app.include_router(assignments.router, prefix=prefix)
    app.include_router(queues.router, prefix=prefix)
    app.include_router(sla.router, prefix=prefix)
    app.include_router(my_work.router, prefix=prefix)
    app.include_router(rules.router, prefix=prefix)
    app.include_router(forms.router, prefix=prefix)
    app.include_router(data_models.router, prefix=prefix)
    app.include_router(form_submissions_router, prefix=prefix)
    app.include_router(analytics_router, prefix=prefix)
    app.include_router(webhooks_router, prefix=prefix)
    app.include_router(admin_router, prefix=prefix)
    app.include_router(auth_router, prefix=prefix)
    app.include_router(process_mining_router, prefix=prefix)
    app.include_router(nlp_router, prefix=prefix)
    app.include_router(scout_router, prefix=prefix)
    app.include_router(tenants_router, prefix=prefix)
    app.include_router(codegen_router, prefix=prefix)
    app.include_router(scout_ai_router, prefix=prefix)
    app.include_router(enterprise_router, prefix=prefix)
    app.include_router(sitemap_router, prefix=prefix)
    app.include_router(orchestrator_router, prefix=prefix)
    app.include_router(realtime_router, prefix=prefix)
    app.include_router(documents_router, prefix=prefix)
    app.include_router(escalation_router, prefix=prefix)
    app.include_router(user_directory_router, prefix=prefix)
    app.include_router(compliance_router, prefix=prefix)
    app.include_router(email_router, prefix=prefix)
    app.include_router(push_router, prefix=prefix)
    app.include_router(hxnexus_router, prefix=prefix)
    app.include_router(portal_router, prefix=prefix)
    app.include_router(portal_admin_router, prefix=prefix)
    app.include_router(portal_customers_public_router, prefix=prefix)
    app.include_router(portal_customers_admin_router, prefix=prefix)
    app.include_router(releases_router, prefix=prefix)
    app.include_router(hxdbmanager_router, prefix=prefix)
    app.include_router(access_groups_router, prefix=prefix)
    app.include_router(hxstream_router, prefix=prefix)
    app.include_router(knowledge_router, prefix=prefix)
    app.include_router(graph_router, prefix=prefix)
    app.include_router(apps_router, prefix=prefix)
    app.include_router(importer_router, prefix=prefix)
    app.include_router(hxbridge_router, prefix=prefix)
    app.include_router(webhook_router, prefix=prefix)
    app.include_router(hxsync_router, prefix=prefix)
    app.include_router(hxglobal_router, prefix=prefix)
    app.include_router(hxshield_router, prefix=prefix)
    app.include_router(hxfusion_router, prefix=prefix)
    app.include_router(payments_router, prefix=prefix)   # P48
    app.include_router(identity_router, prefix=prefix)   # P49
    app.include_router(esign_router,    prefix=prefix)   # P49
    app.include_router(crm_router,      prefix=prefix)   # P50
    app.include_router(invoice_router,  prefix=prefix)   # P50
    app.include_router(comms_router,    prefix=prefix)   # P51
    app.include_router(docintel_router, prefix=prefix)   # P52
    app.include_router(devconn_router,   prefix=prefix)   # P53
    app.include_router(hxmigrate_router, prefix=prefix)   # P54
    app.include_router(hxdeploy_router,  prefix=prefix)   # P55
    app.include_router(hxwork_router,    prefix=prefix)   # P56
    app.include_router(hxcanvas_router,  prefix=prefix)   # P57
    app.include_router(hxdocs_router,    prefix=prefix)   # P58
    app.include_router(branches_router,  prefix=prefix)   # P60 HxBranch
    app.include_router(commits_router,   prefix=prefix)   # P61 Commit pattern
    app.include_router(intake_router,    prefix=prefix)   # Process-Case integration
    app.include_router(hxlogs_router,   prefix=prefix)   # P63 HxLogs
    app.include_router(auth_real_router, prefix=prefix)  # P64 Real Auth
    app.include_router(permissions_router, prefix=prefix)
    app.include_router(marketplace_router, prefix=prefix)  # Marketplace
    app.include_router(checkout_router, prefix=prefix)  # HxCheckout (marketplace app)
    app.include_router(storefront_router, prefix=prefix)  # HxStorefront (marketplace app)
    app.include_router(storefront_public_router, prefix=prefix)  # HxStorefront public storefront
    app.include_router(platform_updates_router, prefix=prefix)  # PUO Phase 1
    app.include_router(case_variables_router, prefix=prefix)  # Case Variables P1
    from case_service.api.routers import testsuite as _testsuite
    app.include_router(_testsuite.router, prefix=prefix)  # Test Suite core (#27)
    from case_service.api.routers import hxtest as _hxtest
    app.include_router(_hxtest.router, prefix=prefix)  # HxTest marketplace AI layer (#27)


    # Middleware (order matters — last added = first executed)
    app.add_middleware(SuperadminGateMiddleware)  # boot gate — must be outermost
    app.add_middleware(AuditMiddleware)            # system-wide action audit
    app.add_middleware(RequestTrackingMiddleware)
    import os
    if not settings.debug and "PYTEST_CURRENT_TEST" not in os.environ:
        # >>> P32 scaling: backend selector
        if getattr(settings, "rate_limit_backend", "memory") == "redis" and getattr(settings, "redis_enabled", False):
            from case_service.middleware.rate_limit_redis import RedisRateLimitMiddleware
            app.add_middleware(
                RedisRateLimitMiddleware,
                requests_per_minute=120, burst=30,
                exclude_paths=["/health", "/ready", "/metrics"],
            )
        else:
            app.add_middleware(
                RateLimitMiddleware,
                requests_per_minute=120, burst=30,
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
    app.add_middleware(SecurityHeadersMiddleware)
    # <<< Group D

    # Health endpoints (no prefix — /health and /ready at root)
    app.include_router(health_router)

    return app


app = create_app()