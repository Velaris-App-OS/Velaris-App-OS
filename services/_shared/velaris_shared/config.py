"""Case-service configuration via environment variables.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import AliasChoices, Field, TypeAdapter, field_validator, model_validator
from pydantic_settings import BaseSettings

_log = logging.getLogger("velaris.config")


class Settings(BaseSettings):
    """Case-service settings loaded from env / .env file."""

    # Service
    service_name: str = "velaris-case-service"
    service_host: str = "0.0.0.0"
    service_port: int = 8200
    debug: bool = False

    # Database (DB SDK — multi-dialect). `database_backend` selects a FIRST-PARTY backend
    # from the baked-in allowlist (velaris_shared.db.backends.ALLOWED_BACKENDS); an unknown
    # value aborts startup. Set once by start-velaris.sh from `velaris.yaml: database:`.
    database_backend: str = Field(
        default="postgresql",
        validation_alias=AliasChoices("VELARIS_CASE_DATABASE_BACKEND", "DATABASE_BACKEND"),
    )
    # Full URL takes precedence when set (current behaviour; trusted operator / OpenBao
    # config). Empty → the backend builds the URL from the typed components below.
    database_url: str = (
        "postgresql+asyncpg://velaris:velaris_dev_password@localhost:5432/velaris"
    )
    # Typed connection components (used only when database_url is empty). Password is never
    # read from velaris.yaml plaintext — only from env / OpenBao.
    db_host: str = ""
    db_port: int | None = None
    db_name: str = ""
    db_user: str = ""
    db_password: str = Field(
        default="",
        validation_alias=AliasChoices("VELARIS_DB_PASSWORD", "VELARIS_CASE_DB_PASSWORD"),
    )
    db_echo: bool = False
    db_pool_size: int = 10           # operations pool (case CRUD, forms, etc.)
    db_max_overflow: int = 5
    db_auth_pool_size: int = 3       # dedicated auth pool — never starved by analytics
    db_analytics_pool_size: int = 3  # analytics / compliance / sync pool
    # R6 soak S9 (2026-11-03): longest an analytics READ statement may run, in
    # ms (PostgreSQL `SET LOCAL statement_timeout`, per read-only transaction, so
    # safe behind PgBouncer). A runaway report then ends with its caller instead
    # of holding locks and CPU for minutes. 0 / unset = no limit, as before.
    db_analytics_read_timeout_ms: int = 0
    # Seconds an analytics answer (dashboard, overview, SLA compliance, cases
    # over time) is reused for the same tenant scope and parameters. Under
    # load every viewer of a tenant then shares one calculation (R6 soak,
    # 2026-11-05: dashboard p95 3.6 s at 400 users). Numbers may be that many
    # seconds old. 0 / unset = off: every request computes, as before. Max 300.
    analytics_cache_seconds: int = 0
    # Optional READ-ONLY replica (hot standby) for heavy analytics queries.
    # Empty = those queries share the analytics pool on the primary. Served
    # via get_replica_session only — the analytics pool itself stays on the
    # primary because compliance seals, hxsync, PUO, and saved-report CRUD
    # write through it and would fail on a standby.
    db_analytics_url: str = ""
    # R5d 11b — the read/write split (agreed 2026-10-22). A read-only replica for
    # the paths that opted in (get_read_session). db_analytics_url wins when both
    # are set, exactly as before. Both empty = every read on the main database.
    database_read_url: str = ""
    # After a user writes, their reads go to the main this long, whatever the path:
    # nobody sees their own change missing.
    read_own_writes_seconds: float = 5.0
    # Replica DOWN (not merely behind): reads go to the main through a pool this
    # small, so they can never crowd out the main's own work…
    read_fallback_pool_size: int = 3
    # …and come back to the replica only after it has been healthy this long.
    read_recovery_seconds: float = 60.0
    # How often each process looks at the replica.
    read_health_seconds: float = 10.0
    # Point-in-time outputs (scheduled reports, exports, snapshots) wait at most
    # this long for the replica to pass their cutoff, then read the main.
    read_barrier_max_seconds: float = 30.0
    # R5d 6a — the connection pooler (PgBouncer, PostgreSQL only). Set → every
    # application pool connects through it. Empty = direct to database_url,
    # exactly as before. database_url stays the DIRECT address: migrations,
    # backups and admin scripts never go through the pooler.
    database_pooler_url: str = ""
    # R5e step 7 — commerce-service's own pool on the same pooler (PgBouncer's
    # `velaris_commerce` database: same database and role, separate server
    # connections), so a commerce transaction waiting on case-service's internal
    # API never holds a connection case-service needs. Read by commerce-service
    # only, and only while database_pooler_url is set. Empty = commerce shares
    # database_pooler_url, exactly as before.
    commerce_database_pooler_url: str = ""
    # R5e step 8 — the same for portal-service (`velaris_portal`): a portal
    # request waits on case-service's internal API inside its transaction too.
    portal_database_pooler_url: str = ""
    # R5e step 10 — the same for user-service (`velaris_user`): a password reset
    # waits on case-service's internal API (the mail) inside its transaction.
    user_database_pooler_url: str = ""
    # The platform reaches the pooler over its UNIX socket (the folder
    # deploy/docker-compose/pooler-socket, 0700, the install owner's) instead of
    # 127.0.0.1:6432. A loopback port is free while the pooler restarts; a local
    # user who bound it then could pose as PostgreSQL and be sent the database
    # password in clear (asyncpg cannot refuse that). The pooler URLs above are
    # not rewritten — only where they connect. False = TCP, exactly as before.
    # No TCP fallback: a missing socket means the pooler is down.
    database_pooler_socket: bool = False

    # Deleting a user (Nov_TODO "User deletion v2"): refused and signed out at
    # once, purged — with everything that grants access — after this many days;
    # an admin can undo in between. 0 = purge at once.
    user_delete_grace_days: int = 30
    # One admin deleting many accounts in an hour: a high alert past the first
    # number, refused past the second (superadmin excepted).
    user_delete_bulk_alert: int = 5
    user_delete_bulk_limit: int = 20
    # Where purged accounts' signed records are kept, outside the database (a
    # restore replays them). Empty = <install>/.deletions. On Kubernetes point it
    # at a persistent volume.
    deletion_ledger_dir: str = ""

    # R5e step 9 — whether this case-service process starts the background jobs
    # (case_service/jobs.py). Unset = yes, exactly as before. Every singleton job
    # runs under a lease, so leaving it on beside the worker service is safe —
    # one of them runs each job. setup writes false for a new install, whose
    # worker service runs them.
    run_jobs: bool = True

    # D2: request body size limits (three tiers)
    max_body_bytes: int = 10 * 1024 * 1024             # JSON payloads
    max_upload_bytes: int = 25 * 1024 * 1024           # multipart uploads (docs, portal, etc.)
    max_migrate_upload_bytes: int = 200 * 1024 * 1024  # hxmigrate BPM exports only

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "velaris-case-queue"
    # Mutual TLS to a Temporal on another server (VELARIS_EXTERNAL_SERVICES=temporal
    # refuses to start without all three). PEM file paths. All empty = plaintext,
    # as before — right for the bundled Temporal on this box's loopback.
    temporal_tls_ca: str = ""
    temporal_tls_cert: str = ""
    temporal_tls_key: str = ""
    # The name on the server's certificate, when it differs from the host in
    # temporal_host (e.g. connecting by IP). Empty = the host.
    temporal_tls_server_name: str = ""

    # F-04 second slice — the simulation TWIN.
    #
    # Both empty means NO TWIN, which is the only safe default and the only
    # behaviour an install taking this update may see: a simulation reports
    # exactly what it reported before the twin existed. Nothing in Velaris
    # starts the twin worker — the operator runs a second case-service worker
    # with `VELARIS_CASE_TASK_QUEUE` set to the queue below and
    # `VELARIS_CASE_DATABASE_URL` set to the database below.
    #
    # They are checked against production before a rehearsal runs, on
    # (host, port, database) and on the queue name. A twin that is actually
    # production would write synthetic cases into real data and clear a
    # promotion with a rehearsal that damaged the thing it rehearsed.
    twin_task_queue: str = ""
    twin_database_url: str = ""
    #: How many synthetic cases one rehearsal drives. Bounded here rather than
    #: taken from the request: a caller who could ask for a million would be a
    #: denial-of-service against the operator's own twin.
    twin_max_cases: int = 200
    #: Seconds one rehearsal may take before it is stopped and reported as
    #: whatever it had reached. A rehearsal that runs forever blocks the
    #: promotion it exists to inform.
    twin_deadline_seconds: int = 300

    # Engine API (for deploying lifecycle processes)
    engine_api_url: str = "http://localhost:8100"

    # NLP settings
    nlp_enabled: bool = True
    nlp_fallback_enabled: bool = True  # Use heuristic fallback if LLM unavailable

    # Auth
    # IMPORTANT: auth_secret MUST be set via VELARIS_CASE_AUTH_SECRET in production.
    # The default is intentionally weak and prints a startup warning if unchanged.
    auth_mode: str = "jwt"  # "jwt" | "oidc"
    auth_secret: str = "velaris-dev-secret-change-in-production"
    auth_issuer: str = "velaris"
    auth_audience: str = "velaris-api"
    token_expiry_days: int = 60             # refresh token lifetime; overridden by velaris_settings DB table
    access_token_expiry_minutes: int = 15   # short-lived access token (override via VELARIS_CASE_ACCESS_TOKEN_EXPIRY_MINUTES)
    oidc_discovery_url: str = ""
    oidc_client_id: str = "velaris-studio"

    # RS256 asymmetric JWT (recommended for production).
    # When both are set, RS256 is used for all token operations and auth_secret is ignored.
    # Generate with: python -c "from velaris_shared.auth.jwt_handler import generate_rsa_keypair; generate_rsa_keypair()"
    # Set VELARIS_CASE_AUTH_RSA_PRIVATE_KEY and VELARIS_CASE_AUTH_RSA_PUBLIC_KEY to the PEM strings.
    # Newlines in env vars must be encoded as literal \n  (most .env loaders handle this).
    auth_rsa_private_key: str = ""  # PEM-encoded RSA-2048 private key (signing only)
    auth_rsa_public_key: str = ""   # PEM-encoded RSA-2048 public key  (verification only)

    # ── Workload identity (roadmap #18) ──────────────────────────────────
    # The gateway is the only door, but "only door" was a routing statement, not
    # an enforced one: every service binds loopback, and any other process on the
    # host could reach 8201-8204 directly and skip whatever the gateway does.
    #
    # When this secret is set, the gateway injects it as a header and each
    # service refuses any request that does not carry it. That makes the gateway
    # the only *caller*, not merely the only route — and it keeps working
    # unchanged if a service moves to another host, which is when it stops being
    # theoretical.
    #
    # Empty (the default) means the check is OFF, so an existing install is not
    # locked out by an upgrade. `setup-velaris.sh` generates one for new installs.
    # Health, readiness and metrics are always exempt: systemd, Docker and the
    # metrics scrape reach those directly and legitimately.
    gateway_identity_secret: str = Field(
        default="", validation_alias="VELARIS_GATEWAY_IDENTITY_SECRET")

    # Internal service API (R5e step 7 — the commerce split). commerce-service
    # asks case-service to do what only case-service may do (create an order's
    # case, advance a case, apply a pack bundle, …) on `/internal/v1/*`, which
    # the gateway never forwards. Each call carries this secret; case-service
    # refuses any call without it. EMPTY = every internal route refuses (503),
    # nothing else changes. setup AND update generate one (pre-customer
    # exception recorded in CLAUDE.md, 2026-10-19).
    internal_service_secret: str = Field(
        default="", validation_alias="VELARIS_INTERNAL_SERVICE_SECRET")
    # Where commerce-service reaches case-service directly — loopback on a
    # single box, the service's cluster address on Kubernetes. Empty = loopback
    # on VELARIS_CASE_PORT.
    case_service_internal_url: str = Field(
        default="", validation_alias="VELARIS_CASE_SERVICE_INTERNAL_URL")

    # Demo tenants (2026-11-04, Nov_TODO §0 "SECURITY"): every website sign-up
    # gets a tenant of its own, made by user-service's
    # `/internal/v1/demo/tenants`. Only tenants that route made (marked
    # `settings.demo`) are ever expired or deleted; nothing else reads these.
    # The ONE credential the website's registration service holds (user's
    # decision 2026-11-04: not the internal service secret, not the gateway
    # identity secret). Accepted only on /internal/v1/demo/*, header
    # X-Velaris-Demo. EMPTY = those routes refuse (503) and nothing else changes;
    # setup-website.sh generates it, on the operator's run.
    demo_provision_secret: str = Field(
        default="", validation_alias="VELARIS_DEMO_PROVISION_SECRET")
    demo_max_tenants: int = Field(default=200, ge=0, validation_alias="VELARIS_DEMO_MAX_TENANTS")
    demo_tenant_days: int = Field(default=7, ge=1, le=365, validation_alias="VELARIS_DEMO_TENANT_DAYS")
    demo_max_users: int = Field(default=5, ge=1, validation_alias="VELARIS_DEMO_MAX_USERS")
    demo_max_cases: int = Field(default=200, ge=1, validation_alias="VELARIS_DEMO_MAX_CASES")
    # Days an expired (archived) demo tenant is kept — time for a sales
    # extension — before the worker deletes it and its accounts.
    demo_delete_after_days: int = Field(default=3, ge=0, le=90,
                                        validation_alias="VELARIS_DEMO_DELETE_AFTER_DAYS")

    # Internal TLS (#18, second half) — OFF by default and deliberately so.
    # Everything currently talks over host loopback behind a loopback-only
    # gateway, so mTLS would encrypt traffic no one can reach while adding a
    # certificate authority to operate. The plumbing is built and the scripts are
    # in deploy/tls/; switch this on when a service actually moves off this host.
    internal_tls_enabled: bool = Field(
        default=False, validation_alias="VELARIS_INTERNAL_TLS_ENABLED")
    internal_tls_dir: str = Field(
        default="deploy/tls/certs", validation_alias="VELARIS_INTERNAL_TLS_DIR")

    # HxVault (#19) — master Key-Encryption-Key that wraps per-tenant DEKs.
    # 32 bytes, hex (64 chars) or base64. MUST be set via VELARIS_CASE_KEK in
    # production (rendered from OpenBao). New env vars use the VELARIS_CASE_ prefix
    # (field-level alias; the class default prefix is still VELARIS_CASE_ for legacy
    # vars). When empty, a dev KEK is derived from auth_secret (round-trips fine;
    # crypto-shred still works since DEKs are random+stored) + startup warning.
    kek: str = Field(default="", validation_alias="VELARIS_CASE_KEK")
    # HxVault multi-worker coherence: how often each worker reconciles its in-process
    # DEK cache with the DB (picks up DEKs created on other workers, evicts shredded
    # ones). Lower = faster cross-worker propagation, more DB reads. 0 disables.
    vault_cache_resync_seconds: int = Field(default=30, validation_alias="VELARIS_CASE_VAULT_RESYNC_SECONDS")

    # >>> P24 document storage
    # "local" (default) | "s3" | "minio" — the last two are the same S3 client;
    # the endpoint is the minio_* settings below, whatever service answers it.
    storage_backend: str = "local"
    storage_local_path: str = Field(
        default="./data/velaris-docs",  # relative to working dir
        # Explicit alias is redundant now that the class prefix is VELARIS_CASE_,
        # but kept so the name stays greppable at the field it configures.
        validation_alias="VELARIS_CASE_STORAGE_LOCAL_PATH",
    )
    # The S3 endpoint. VELARIS_CASE_S3_* are the names from v3.1.2; the
    # VELARIS_CASE_MINIO_* names every earlier install may have in .env are read
    # for good — never remove them. When both are set the S3 name wins.
    s3_endpoint: str = Field(
        default="localhost:9000",
        validation_alias=AliasChoices("VELARIS_CASE_S3_ENDPOINT", "VELARIS_CASE_MINIO_ENDPOINT"),
    )
    s3_access_key: str = Field(
        default="velaris",
        validation_alias=AliasChoices("VELARIS_CASE_S3_ACCESS_KEY", "VELARIS_CASE_MINIO_ACCESS_KEY"),
    )
    # Empty = not set. Resolved by `s3_secret_key` below, never read directly.
    s3_secret_key_configured: str = Field(
        default="",
        validation_alias=AliasChoices("VELARIS_CASE_S3_SECRET_KEY", "VELARIS_CASE_MINIO_SECRET_KEY"),
    )
    s3_bucket: str = Field(
        default="velaris-docs",
        validation_alias=AliasChoices("VELARIS_CASE_S3_BUCKET", "VELARIS_CASE_MINIO_BUCKET"),
    )
    s3_secure: bool = Field(
        default=False,
        validation_alias=AliasChoices("VELARIS_CASE_S3_SECURE", "VELARIS_CASE_MINIO_SECURE"),
    )
    # The bundled MinIO container's root password, as setup wrote it for the
    # compose file. Read here only as the fallback in `s3_secret_key`.
    bundled_minio_password: str = Field(default="", validation_alias="VELARIS_MINIO_PASSWORD")

    @property
    def s3_secret_key(self) -> str:
        """The secret the S3 client signs with.

        1. the configured secret (VELARIS_CASE_S3_SECRET_KEY, or the MINIO name);
        2. else, for an endpoint ON THIS MACHINE, the bundled container's own
           password. From 2026-08-06 setup wrote STORAGE_BACKEND=minio and a
           random VELARIS_MINIO_PASSWORD for the container, and never told the
           service: it signed with the dev default below, MinIO answered
           SignatureDoesNotMatch, and document storage was dead on every install
           made that way (found 2026-10-12). Never used for a remote endpoint —
           one service's password is not another's;
        3. else the historical dev default, so nothing that worked stops.
        """
        if self.s3_secret_key_configured:
            return self.s3_secret_key_configured
        host = str(self.s3_endpoint).rsplit(":", 1)[0].strip("[]").lower()
        if self.bundled_minio_password and host in ("localhost", "127.0.0.1", "::1", "minio"):
            return self.bundled_minio_password
        return "velaris_dev_password"

    # The attribute names from before v3.1.2, for anything that still reads them.
    @property
    def minio_endpoint(self) -> str:
        return self.s3_endpoint

    @property
    def minio_access_key(self) -> str:
        return self.s3_access_key

    @property
    def minio_secret_key(self) -> str:
        return self.s3_secret_key

    @property
    def minio_bucket(self) -> str:
        return self.s3_bucket

    @property
    def minio_secure(self) -> bool:
        return self.s3_secure

    # At-rest encryption for document storage (local and MinIO).
    # Set VELARIS_CASE_STORAGE_MASTER_KEY to a 64-char hex string (32 bytes).
    # Generate with: openssl rand -hex 32
    # When set, every file is encrypted with AES-256-GCM before being written
    # to disk or MinIO. Admins with direct filesystem/bucket access see only
    # ciphertext. Empty = encryption disabled (dev default).
    storage_master_key: str = ""
    # <<< P24 document storage

    # >>> HxMeet P1 (real-time case sessions)
    # Platform-default session driver. P1 ships off_platform only (Teams/Zoom/
    # Meet/generic connector creates the meeting); embedded (LiveKit) is P2.
    # Per-tenant override lives in tenant.settings["meet"] (driver / provider /
    # connector_id).
    meet_driver: str = Field(default="off_platform", validation_alias="VELARIS_CASE_MEET_DRIVER")
    # <<< HxMeet P1

    # >>> HxMeet P2 (embedded driver — self-hosted LiveKit)
    # The embedded driver is available only when all three LiveKit settings
    # are set; otherwise selecting it fails closed with 501. The API secret
    # is server-side only — the browser only ever sees a minted, room-scoped,
    # short-TTL access token.
    livekit_url: str = Field(default="", validation_alias="VELARIS_CASE_LIVEKIT_URL")
    livekit_api_key: str = Field(default="", validation_alias="VELARIS_CASE_LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", validation_alias="VELARIS_CASE_LIVEKIT_API_SECRET")
    # TTLs: room access tokens are minted per join, minutes-lived; guest
    # invite links are single-use and expire independently.
    meet_token_ttl_seconds: int = Field(default=300, validation_alias="VELARIS_CASE_MEET_TOKEN_TTL")
    meet_guest_invite_ttl_seconds: int = Field(default=900, validation_alias="VELARIS_CASE_MEET_GUEST_INVITE_TTL")
    # Where **Studio** is reachable from a browser, e.g.
    # "https://velaris.example.com" — not this API's own address.
    #
    # It matters because the pages a guest lands on are Studio routes:
    # `/meet/join?token=…` is served by the front end, which in this deployment
    # runs on its own port (5173) behind whatever the operator puts in front of
    # it. Falling back to the origin of the API request would post a link to a
    # page the API does not serve, and for an external guest it would point at a
    # host they cannot reach at all.
    #
    # Unset, anything that needs a guest link says so instead of emailing one
    # that cannot work.
    public_base_url: str = Field(default="", validation_alias="VELARIS_CASE_PUBLIC_BASE_URL")
    # <<< HxMeet P2

    # >>> HxMeet P4a (session intelligence) — local Whisper model size
    meet_whisper_model: str = Field(default="base", validation_alias="VELARIS_CASE_MEET_WHISPER_MODEL")
    # <<< HxMeet P4a

    # >>> HxMeet P4a-live (streaming captions)
    # ASR engine selection: auto probes the machine (NVIDIA>CUDA, AMD/Intel
    # GPU>whisper.cpp Vulkan, else CPU lag mode); override only to pin for
    # support (auto | cuda | vulkan | rocm | cpu).
    meet_asr_backend: str = Field(default="auto", validation_alias="VELARIS_CASE_MEET_ASR_BACKEND")
    # Live model = the latency/accuracy dial; post-session P4a keeps its own.
    meet_asr_live_model: str = Field(default="small", validation_alias="VELARIS_CASE_MEET_ASR_LIVE_MODEL")
    # <<< HxMeet P4a-live

    # >>> HxMeet P3 (sealed recording)
    # Shared directory where the LiveKit egress worker drops finished
    # recordings and case-service picks them up for sealing (sha256 >
    # tenant-DEK seal > case document > audit chain). The file is deleted
    # after ingest. Empty = recording endpoints fail closed with 501.
    meet_recordings_dir: str = Field(default="", validation_alias="VELARIS_CASE_MEET_RECORDINGS_DIR")
    # HTTP base of the LiveKit server for Egress API calls (Twirp). Defaults
    # to livekit_url with ws>http scheme swap when unset.
    livekit_http_url: str = Field(default="", validation_alias="VELARIS_CASE_LIVEKIT_HTTP_URL")
    # <<< HxMeet P3

    # >>> HxGuard Phase B (env: VELARIS_CASE_HXGUARD_CASE_ENFORCEMENT)
    # off    = case-level ReBAC checks skipped entirely
    # shadow = evaluated + would-be denials audited, requests pass
    # enforce = denials return 404 (anti-oracle) — the DEFAULT
    #
    # Defaults to enforce because the rollout it was built for is over. In
    # shadow EVERY case gate is advisory, including meet.recording.view, so a
    # fresh install that never sets this env var would serve sealed recordings
    # and transcripts to any authenticated user. A deployment mid-migration can
    # still opt back to shadow explicitly; silence must not mean "open".
    hxguard_case_enforcement: str = "enforce"
    #: Phase 2 of privilege-based authorization: evaluate the privilege model
    #: alongside every `require_role` gate and record disagreements. Observation
    #: only — it never changes a decision. Off switches the comparison off
    #: entirely (the gate is unaffected either way).
    hxguard_privilege_shadow: bool = True
    # <<< HxGuard Phase B

    # >>> HxNexus Operator (MCP) P1
    # ai_tools=False exposes only the deterministic tool profile (tools/list
    # omits AI-backed tools and calling one is rejected) — MCP keeps working
    # where AI egress is disabled or the LLM backend is down.
    mcp_ai_tools: bool = Field(default=True, validation_alias="VELARIS_CASE_MCP_AI_TOOLS")
    mcp_rate_per_min: int = Field(default=30, validation_alias="VELARIS_CASE_MCP_RATE_PER_MIN")
    # P2 writes are opt-in: mutating tools are hidden from tools/list AND
    # rejected on call until an operator turns them on. "AI that acts" is the
    # riskier surface, so default-off is the safe posture.
    mcp_writes_enabled: bool = Field(default=False, validation_alias="VELARIS_CASE_MCP_WRITES_ENABLED")
    # P3 stateful lifecycle actions (advance/status/close/create) — separate,
    # higher-risk opt-in; and by default they require human confirmation (the
    # AI proposes, a human executes) until the injection posture is proven.
    mcp_stateful_enabled: bool = Field(default=False, validation_alias="VELARIS_CASE_MCP_STATEFUL_ENABLED")
    mcp_confirm_stateful: bool = Field(default=True, validation_alias="VELARIS_CASE_MCP_CONFIRM_STATEFUL")
    mcp_proposal_ttl_minutes: int = Field(default=60, validation_alias="VELARIS_CASE_MCP_PROPOSAL_TTL_MINUTES")
    # P4 external agents: per-tool short-lived scoped tokens. Master switch is
    # default-off AND acts as a kill switch — turning it off instantly rejects
    # every outstanding scoped token, not just new mints.
    mcp_external_tokens_enabled: bool = Field(default=False, validation_alias="VELARIS_CASE_MCP_EXTERNAL_TOKENS_ENABLED")
    mcp_external_rate_per_min: int = Field(default=15, validation_alias="VELARIS_CASE_MCP_EXTERNAL_RATE_PER_MIN")
    mcp_token_default_ttl_minutes: int = Field(default=30, validation_alias="VELARIS_CASE_MCP_TOKEN_DEFAULT_TTL_MINUTES")
    mcp_token_max_ttl_minutes: int = Field(default=60, validation_alias="VELARIS_CASE_MCP_TOKEN_MAX_TTL_MINUTES")

    # ── F-00 agent identity ───────────────────────────────────────────────
    # Agent principals may always be created and inspected; what this switch
    # gates is the minting and ACCEPTANCE of agent tokens. Default-off and a
    # true kill switch, exactly like `mcp_external_tokens_enabled`: turning it
    # off rejects every outstanding agent token on its next call, not merely
    # new mints. An install taking the update has it off and nothing changes.
    agent_tokens_enabled: bool = Field(default=False, validation_alias="VELARIS_CASE_AGENT_TOKENS_ENABLED")
    agent_token_default_ttl_minutes: int = Field(default=15, validation_alias="VELARIS_CASE_AGENT_TOKEN_DEFAULT_TTL_MINUTES")
    # Deliberately shorter than the MCP ceiling. An agent runs unattended, so a
    # leaked token has nobody watching; the design's "≤ 24 h" is the outer bound
    # for a case-scoped work order, not a sensible default for a worker.
    agent_token_max_ttl_minutes: int = Field(default=60, validation_alias="VELARIS_CASE_AGENT_TOKEN_MAX_TTL_MINUTES")

    # ── F-05 agent workforce: the scheduled scorecard sweep ───────────────
    # Default OFF. The sweep only recomputes what `POST /agents/{id}/workforce/
    # scorecard` already computes on demand, and it is inert on an install with
    # an empty roster — which every install has, because enrolment is an
    # explicit act. Off anyway: an update does not get to start a background
    # writer on somebody's box, and an operator who wants nightly reviews turns
    # them on and picks the cadence.
    agent_scorecard_cron_enabled: bool = Field(default=False, validation_alias="VELARIS_CASE_AGENT_SCORECARD_CRON_ENABLED")
    # Doubles as the de-duplication window: an agent whose newest scorecard is
    # younger than this is skipped, so restarts cannot fill the table.
    agent_scorecard_interval_hours: int = Field(default=24, validation_alias="VELARIS_CASE_AGENT_SCORECARD_INTERVAL_HOURS")
    agent_scorecard_window_days: int = Field(default=7, validation_alias="VELARIS_CASE_AGENT_SCORECARD_WINDOW_DAYS")

    # ── F-06 ambient capture: the automatic sweep ─────────────────────────
    # Two switches, both off, and they are not redundant. THIS one is the
    # operator's — whether this box runs a background reader of mail and
    # meeting transcripts at all. The tenant's own
    # `settings["capture"]["auto"]` decides whether their sources are read.
    # A multi-tenant install needs both answers and neither implies the other.
    capture_auto_enabled: bool = Field(default=False, validation_alias="VELARIS_CASE_CAPTURE_AUTO_ENABLED")
    capture_auto_interval_seconds: int = Field(default=300, validation_alias="VELARIS_CASE_CAPTURE_AUTO_INTERVAL_SECONDS")
    # The upgrade-safety property, not a performance one: without it the first
    # cycle after somebody switches this on would read every inbound email ever
    # threaded to a case. Turning capture on starts watching; it does not
    # reprocess the archive.
    capture_auto_lookback_hours: int = Field(default=24, validation_alias="VELARIS_CASE_CAPTURE_AUTO_LOOKBACK_HOURS")
    capture_auto_max_per_cycle: int = Field(default=20, validation_alias="VELARIS_CASE_CAPTURE_AUTO_MAX_PER_CYCLE")
    # <<< HxNexus Operator (MCP) P1

    # >>> P32 scaling
    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    # The cache's AUTH password (VELARIS_CASE_REDIS_PASSWORD). Kept out of the URL
    # so it is never logged. Empty = no AUTH, as before.
    redis_password: str = Field(default="", repr=False)
    redis_channel_prefix: str = "velaris:rt:"
    rate_limit_backend: str = "memory"  # "memory" | "redis"
    # <<< P32 scaling

    # >>> Unified AI backend (used by HxNexus, NLP Builder, Scout AI)
    #
    # SIMPLE (new) — set just these 3-4 vars for any provider:
    #   VELARIS_CASE_AI_PROVIDER = groq | mistral | gemini | together | openai | anthropic | ollama | custom
    #   VELARIS_CASE_AI_API_KEY  = your API key
    #   VELARIS_CASE_AI_MODEL    = model name (e.g. llama-3.1-70b-versatile)
    #   VELARIS_CASE_AI_EMBED_MODEL = embedding model (optional, falls back to ollama)
    #   VELARIS_CASE_AI_BASE_URL = only needed for 'custom' or to override a known provider
    #
    # LEGACY (still supported) — existing vars keep working as before.
    #
    # Group E: AI egress policy — local_only | minimized | full
    #   local_only — external AI providers refused; local Ollama used instead
    #   minimized  — external completions allowed, embeddings always stay local
    #   full       — external provider used as-is (explicit opt-in)
    ai_egress_policy: str = "minimized"

    # Group H: chunk minimization when completions go to an external provider
    ai_egress_top_k: int = 3                    # retrieved chunks sent externally (local uses 5)
    ai_egress_min_score: float = 0.25           # relevance floor for external context
    ai_egress_max_context_chars: int = 6000     # total context cap per external call

    # HxNexus document Q&A — how a document is cut, and how hits are ordered.
    # Both default to what every install already does; neither re-indexes
    # anything. An unrecognised value is treated as the default, never an error.
    #   hxnexus_chunk_strategy: "" | window — fixed 512-char windows
    #                           structure   — headings > paragraphs > sentences;
    #                                         applies to documents indexed AFTER
    #                                         it is set
    #   hxnexus_rerank:         "" | off    — cosine top-k
    #                           lexical     — cosine blended with a keyword
    #                                         (BM25) score; local, no model call
    hxnexus_chunk_strategy: str = ""
    hxnexus_rerank: str = ""
    hxnexus_rerank_candidates: int = 20         # retrieved before the rerank
    hxnexus_rerank_lexical_weight: float = 0.3  # keyword share of the blend, 0..1

    # §5.3 LLM hardening: model-DoS guard — max chars (prompt + system) per LLM
    # call, enforced at the universal choke point. ~50k tokens. Configurable.
    ai_max_prompt_chars: int = 200_000

    ai_provider: str = ""                   # universal provider name (overrides ai_backend when set)
    ai_api_key: str = ""                    # universal API key
    ai_model: str = ""                      # universal model name
    ai_embed_model: str = ""               # universal embed model (optional)
    ai_base_url: str = ""                  # universal base URL (for custom or override)

    # Legacy vars (backwards compat — keep working)
    ai_backend: str = "ollama"              # "ollama" | "openai" | "anthropic"
    ai_ollama_url: str = "http://localhost:11434"
    ai_ollama_model: str = "llama3.2"
    ai_ollama_embed_model: str = "nomic-embed-text"
    # Bearer key for an Ollama on another server. Ollama has no authentication of
    # its own; a proxy in front of it checks this key. Empty = no header, as before.
    ai_ollama_api_key: str = Field(default="", repr=False)
    # CA file for that proxy's certificate when it is not publicly trusted.
    # Empty = the usual public CAs, as before.
    ai_ollama_tls_ca: str = ""
    ai_openai_api_key: str = ""
    ai_openai_model: str = "gpt-4o-mini"
    ai_openai_embed_model: str = "text-embedding-3-small"
    ai_anthropic_api_key: str = ""
    ai_anthropic_model: str = "claude-haiku-4-5-20251001"
    # <<< Unified AI backend

    # >>> Connector executor limits
    connector_pagination_default_size: int = 50
    connector_overflow_threshold_bytes: int = 256 * 1024   # 256 KB → S3 overflow
    connector_hard_reject_bytes: int = 16 * 1024 * 1024   # 16 MB → reject + DLQ
    # <<< Connector executor limits

    # >>> Marketplace
    # marketplace_enabled is now version-based — controlled via the Release Scheduler
    # in the admin dashboard (releases.py / is_feature_enabled("marketplace")).
    # This kill switch is kept for emergency use only (set false to hard-disable).
    marketplace_kill_switch: bool = False
    marketplace_dev_only: bool = True
    # Roadmap #27 conformance gate: off | warn | enforce.
    # Defaults to `warn` deliberately — an existing platform that upgrades must
    # not begin refusing installs purely because new code shipped. Flip to
    # `enforce` once publishers have had the grandfather window to submit
    # results (same shadow-then-enforce path HxGuard used for case enforcement).
    marketplace_conformance_gate: str = Field(
        default="warn", validation_alias="VELARIS_CASE_MARKETPLACE_CONFORMANCE_GATE"
    )
    # Temporal `definition_json` interpreter (Tier-1 §12 / Tier-2 #21).
    # Option A of docs/Future/temporal-decision-record.md. OFF by default AND
    # requiring the case type to declare `"orchestration": "temporal"` — a
    # workflow that drives case advancement must never switch itself on for
    # case types authored without it.
    temporal_interpreter_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_TEMPORAL_INTERPRETER_ENABLED"
    )
    # Event sourcing for the Case aggregate (#30a / §2.2). OFF by default and
    # a CLEAN CUTOVER when switched on: cases created from that point carry
    # `event_sourced=True` and get a full append-only stream; earlier cases are
    # deliberately NOT backfilled (§7.5 — their history was never recorded as a
    # stream, so reconstructing it would produce a guess dressed as a fact).
    event_sourcing_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_EVENT_SOURCING_ENABLED"
    )
    # HxStatus (§8.8) — periodic health sampling. ON by default: the status
    # page is worthless without a history, the cost is one tiny insert per
    # component per interval, and pruning keeps the table bounded.
    health_sampler_enabled: bool = Field(
        default=True, validation_alias="VELARIS_CASE_HEALTH_SAMPLER_ENABLED"
    )
    health_sampler_interval_seconds: int = Field(
        default=60, validation_alias="VELARIS_CASE_HEALTH_SAMPLER_INTERVAL"
    )
    # Slightly longer than the 30d reporting window, so a 30-day figure is
    # never computed from a partially-pruned history.
    health_sampler_retention_days: int = Field(
        default=35, validation_alias="VELARIS_CASE_HEALTH_SAMPLER_RETENTION_DAYS"
    )
    # Retention scheduler. TWO switches on purpose: the first only reports
    # what would go, the second actually deletes. A single flag is easy to
    # flip while skim-reading a config, and this one destroys customer data.
    # There is deliberately no "enabled implies apply".
    retention_scheduler_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_RETENTION_SCHEDULER_ENABLED"
    )
    retention_scheduler_apply: bool = Field(
        default=False, validation_alias="VELARIS_CASE_RETENTION_SCHEDULER_APPLY"
    )
    retention_scheduler_interval_seconds: int = Field(
        default=86_400, validation_alias="VELARIS_CASE_RETENTION_SCHEDULER_INTERVAL"
    )
    # HxPrivacy (§8.10) — scheduled PII scan. ON by default for the same
    # reason as the health sampler: an inventory nobody refreshes still looks
    # authoritative. Daily, because forms are edited by people, not traffic;
    # the scanner floors this at 1h so it cannot be turned into a load test.
    pii_scanner_enabled: bool = Field(
        default=True, validation_alias="VELARIS_CASE_PII_SCANNER_ENABLED"
    )
    pii_scanner_interval_seconds: int = Field(
        default=86_400, validation_alias="VELARIS_CASE_PII_SCANNER_INTERVAL"
    )
    # HxPrivacy — redact confirmed personal data from exports that LEAVE the
    # tenant (billing, saved analytics reports). Deliberately NOT applied to
    # the GDPR Article 15 export, whose entire purpose is to disclose that data
    # to the subject it belongs to.
    pii_redact_exports: bool = Field(
        default=False, validation_alias="VELARIS_CASE_PII_REDACT_EXPORTS"
    )
    # HxPrivacy, further surfaces (Oct item 11). Each is its own switch and each
    # is OFF unless set — none of them is implied by `pii_redact_exports`, so an
    # install that already redacts its billing export sees no change on upgrade.
    #   table exports — the Database Manager's raw table export
    #   AI egress     — values of confirmed fields masked wherever they appear in
    #                   text sent to an EXTERNAL provider (restored in the answer)
    #   case text     — retention's `anonymize` also blanks the case's messages,
    #                   channel messages and HxNexus conversations. Irreversible.
    pii_redact_table_exports: bool = Field(
        default=False, validation_alias="VELARIS_CASE_PII_REDACT_TABLE_EXPORTS"
    )
    pii_mask_ai_egress: bool = Field(
        default=False, validation_alias="VELARIS_CASE_PII_MASK_AI_EGRESS"
    )
    retention_anonymize_case_text: bool = Field(
        default=False, validation_alias="VELARIS_CASE_RETENTION_ANONYMIZE_CASE_TEXT"
    )

    # HxLedger (§8.4) — post captured payments and refunds into the books.
    # ON by default, for the same reason the health sampler is: a ledger with
    # no postings is worthless, and `/ledger/reconcile` reports every captured
    # payment as a discrepancy until something keeps it. Purely additive — it
    # writes new rows and changes no existing payment path.
    ledger_posting_enabled: bool = Field(
        default=True, validation_alias="VELARIS_CASE_LEDGER_POSTING_ENABLED"
    )
    # Durable event log (§2.1) — fan-out of the transactional outbox onto
    # Kafka so consumers other than webhooks can read the stream.
    # OFF by default: the outbox is already safe on disk, so this adds
    # distribution, not durability, and it should be a deliberate act.
    event_bus_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_EVENT_BUS_ENABLED"
    )
    # 19092 is the EXTERNAL listener. The broker advertises `events:9092` on
    # its internal listener, which a host-run case-service cannot resolve —
    # it bootstraps and then fails DNS. Containers use events:9092.
    event_bus_brokers: str = Field(
        default="127.0.0.1:19092", validation_alias="VELARIS_CASE_EVENT_BUS_BROKERS"
    )
    event_bus_topic_prefix: str = Field(
        default="velaris", validation_alias="VELARIS_CASE_EVENT_BUS_TOPIC_PREFIX"
    )
    # Broker authentication (SASL/PLAIN). The bundled Kafka refuses to start
    # without a password; setup generates one, and start/update generate one
    # when the bus is turned on without it. Empty = connect without SASL, which
    # is only right for a broker the operator runs without authentication.
    # Operator alerts by email (backup and update watchers). A SYSTEM mailbox,
    # separate from tenants' own mail accounts. Unset host or recipients = no
    # email, exactly as before (push and Studio still show the alert).
    alert_email: str = Field(default="", validation_alias="VELARIS_CASE_ALERT_EMAIL")  # comma-separated
    alert_smtp_host: str = Field(default="", validation_alias="VELARIS_CASE_ALERT_SMTP_HOST")
    alert_smtp_port: str = Field(default="", validation_alias="VELARIS_CASE_ALERT_SMTP_PORT")
    alert_smtp_username: str = Field(default="", validation_alias="VELARIS_CASE_ALERT_SMTP_USERNAME")
    alert_smtp_password: str = Field(default="", validation_alias="VELARIS_CASE_ALERT_SMTP_PASSWORD", repr=False)
    alert_smtp_from: str = Field(default="", validation_alias="VELARIS_CASE_ALERT_SMTP_FROM")
    # starttls (default) | ssl | none — "none" only for a relay on this host.
    alert_smtp_tls: str = Field(default="starttls", validation_alias="VELARIS_CASE_ALERT_SMTP_TLS")
    event_bus_username: str = Field(
        default="velaris", validation_alias="VELARIS_CASE_EVENT_BUS_USERNAME"
    )
    event_bus_password: str = Field(
        default="", validation_alias="VELARIS_CASE_EVENT_BUS_PASSWORD", repr=False
    )
    # CA file for a broker on another server. Set = SASL_SSL (the password is
    # then sent only over TLS); empty = SASL_PLAINTEXT, as before — right for the
    # bundled broker on this box's loopback.
    event_bus_tls_ca: str = Field(
        default="", validation_alias="VELARIS_CASE_EVENT_BUS_TLS_CA"
    )
    # HxSandbox (#17) — execution of customer-authored WASM modules.
    # OFF by default and deliberately so: turning this on lets a marketplace
    # package's own compiled code run in-process (fuel/memory/wall-clock capped,
    # no host imports). While off, `runtime: wasm` packages are refused at
    # INSTALL, which is where roadmap #15 put the refusal — never at first run.
    hxsandbox_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_HXSANDBOX_ENABLED"
    )
    # HxShield live detection. **Default off**, and the flag matters more than
    # usual here: the engine has eight detection patterns that have never run
    # against real traffic, because until 2026-08-24 nothing on a request path
    # called it — `evaluate()` had a single caller, the explicit `POST
    # /hxshield/score`. Turning eight untested rules on unattended across a
    # fleet is how a quiet platform becomes an incident storm at 3am, so an
    # operator switches it on, watches what fires, and then decides.
    hxshield_detect_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_HXSHIELD_DETECT_ENABLED"
    )
    # RPA bot runner. **Default off**: this loop starts containers on the host,
    # so it is an opt-in per deployment rather than a platform default. While
    # off, definitions and jobs can still be created and reviewed — they simply
    # queue, which is visible, instead of failing somewhere obscure.
    bot_runner_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_BOT_RUNNER_ENABLED"
    )
    # Where a bot container reaches us to POST its result. It sits on the
    # internal `velaris-apps` network, so this is the address of the ONE
    # container bridging that network — NOT a public URL, and not derived from
    # the request.
    #
    # It must name a container ATTACHED to `velaris-apps`, because docker's
    # embedded DNS only resolves those. `velaris-gateway` is `network_mode:
    # host` and therefore has no record there: pointing at it made every job
    # time out with "the bot did not return", which is what a container has to
    # be started to notice. `velaris-broker-gw` is dual-homed by design and
    # allowlists exactly the result and progress paths.
    bot_result_base_url: str = Field(
        default="http://velaris-broker-gw",
        validation_alias="VELARIS_CASE_BOT_RESULT_BASE_URL",
    )
    # The scheduler loop. **On by default**, unlike the bot runner: it writes
    # rows and sends mail rather than starting containers, and what it can fire
    # is bounded by the approval gate — with nothing approved, an enabled loop
    # does nothing at all.
    scheduler_enabled: bool = Field(
        default=True, validation_alias="VELARIS_CASE_SCHEDULER_ENABLED"
    )
    # How many bot jobs this process may run at once. **This is the host-wide
    # ceiling**: a worker only exists while it holds a job, so N workers is N
    # containers, and the per-bot and per-tenant limits sit underneath it.
    # Sizing: concurrency = (jobs x average runtime) / window. 1,000 two-minute
    # jobs across an eight-hour night is ~5 concurrent; the same 1,000 at five
    # minutes each is ~11. Multiply by the profile's memory to size the box.
    # Default 2 — deliberately low, because the cost of raising it is visible
    # (RAM) and the cost of a default that overcommits a small box is not.
    bot_runner_workers: int = Field(
        default=2, validation_alias="VELARIS_CASE_BOT_RUNNER_WORKERS")
    # Memory ceiling for a `browser` bot. It needs its own, above the Layer-2
    # default: a browser profile's tmpfs is sized 512m + 256m, and **tmpfs pages
    # count against the container's own memory cgroup** — so a 512m limit means a
    # bot that fills its scratch space OOM-kills itself, in a way that looks like
    # a flaky website rather than a misconfiguration.
    bot_browser_mem_limit: str = Field(
        default="1536m", validation_alias="VELARIS_CASE_BOT_BROWSER_MEM_LIMIT")
    # How long one scheduled action may take before it is abandoned. A
    # scheduler runs actions one after another, so an action that never returns —
    # a connector against a supplier's site that holds the connection open, a
    # sync against an unreachable warehouse — would stall EVERY other schedule
    # indefinitely, silently. The timeout is what keeps one bad night from
    # stopping the timetable.
    scheduler_action_timeout_seconds: int = Field(
        default=300, validation_alias="VELARIS_CASE_SCHEDULER_ACTION_TIMEOUT"
    )
    # A tenant-wide ceiling on concurrent bot jobs. `max_concurrency` bounds one
    # definition; without this, twenty definitions each at their own limit could
    # fill the host between them — and a bot is a container, not a coroutine.
    # A `bot_tenant_limits` row overrides this per tenant.
    bot_tenant_max_concurrent: int = Field(
        default=4, validation_alias="VELARIS_CASE_BOT_TENANT_MAX_CONCURRENT"
    )
    # Calendar feed subscriptions ("add a calendar by URL"). A comma-separated
    # allowlist of hosts this server may fetch a calendar from — an exact host,
    # or a leading dot for a domain and its subdomains (`.google.com`).
    #
    # **Unset means no subscription can be created at all**, which is exactly
    # today's behaviour on every existing install: the feature arrives off, and
    # naming the hosts is an operator's decision, not an update's. It is an
    # allowlist rather than a blocklist because the URL comes from a user and
    # the fetch happens from inside the network — the shape of every SSRF.
    calendar_feed_hosts: str = Field(
        default="", validation_alias="VELARIS_CASE_CALENDAR_FEED_HOSTS")

    # ── HxShield response layer (roadmap #25) ────────────────────────────
    # OFF. With this unset, every response HxShield decides on is RECORDED and
    # none is performed — an operator reads a week of "this is what would have
    # happened" before anything acts. An automated response that surprises its
    # operator is switched off permanently on its first false positive, so it
    # has to earn the switch. Turning it on is an operator's decision and
    # `update-velaris.sh` never makes it.
    #
    # Even ON, a DESTRUCTIVE response still waits for a person: this switch
    # governs the automatic ones (notify, quarantine), never suspension or
    # session revocation.
    shield_response_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_SHIELD_RESPONSE_ENABLED")

    # ── OCR (market gap A1) ──────────────────────────────────────────────
    # "" or "stub" = no OCR, which is what this platform has always done: the
    # only engine was a stub returning an empty string, so a SCANNED page
    # yielded nothing and nothing said so. "tesseract" runs the local binary —
    # the sovereignty-preserving choice, since OCR is the classic thing to send
    # to somebody else's cloud.
    #
    # Off by default even where the binary is installed: switching it on changes
    # what a document CONTAINS as far as search, HxPrivacy's scan and every AI
    # path are concerned, and that is an operator's decision, not an update's.
    ocr_engine: str = Field(
        default="", validation_alias="VELARIS_CASE_OCR_ENGINE")
    ocr_language: str = Field(
        default="eng", validation_alias="VELARIS_CASE_OCR_LANGUAGE")
    # A calendar, not a download. Bounds the read itself, so a host that streams
    # forever cannot hold a worker open or fill memory.
    calendar_feed_max_bytes: int = Field(
        default=2_097_152, validation_alias="VELARIS_CASE_CALENDAR_FEED_MAX_BYTES")
    calendar_feed_timeout_seconds: int = Field(
        default=20, validation_alias="VELARIS_CASE_CALENDAR_FEED_TIMEOUT")

    # Max concurrent sandbox workspaces per developer (raise based on available RAM/CPU).
    marketplace_max_workspaces_per_user: int = 2
    # URL of the official Velaris index file (sources.json) — override to self-host.
    # Lives in the official/ folder of the Velaris-App-OS/Marketplace repo (write-
    # protected; only the Velaris org can publish there).
    marketplace_index_url: str = (
        "https://raw.githubusercontent.com/Velaris-App-OS/Marketplace/main/official/sources.json"
    )
    # URL of the community index (community/ folder — fork + PR; anyone contributes).
    # Seeded as a Community-tier source. Empty string disables community seeding.
    marketplace_community_index_url: str = (
        "https://raw.githubusercontent.com/Velaris-App-OS/Marketplace/main/community/sources.json"
    )
    # GitHub org (or orgs) whose source URLs are treated as Official tier.
    # Comma-separated. Any source URL not matching these is always Community.
    # GitHub org names (comma-separated) whose source URLs are Official.
    # Just the org name — works for both github.com and raw.githubusercontent.com URLs.
    # NOTE: org membership is necessary but NOT sufficient — a package is Official
    # only if it is ALSO listed in case_service/marketplace/official_registry.json.
    # (Official and Community share this org, split by folder, so the registry is
    # the real disambiguator.)
    marketplace_official_orgs: str = "velaris-app-os"
    # Optional Velaris security report webhook — opt-in only, no customer data sent.
    marketplace_report_webhook: str = ""
    marketplace_report_webhook_secret: str = ""
    # How often to poll community/private sources for new versions (hours).
    marketplace_poll_interval_hours: int = 6
    # How often to poll official Velaris sources (hours) — shorter because they are always up.
    marketplace_official_poll_interval_hours: int = 1
    # Sandbox workspace auto-expiry (days).
    marketplace_workspace_expiry_days: int = 30
    # >>> Marketplace Layer-2 (execution & trust model)
    # Registries a Layer-2 image may be pulled from (comma-separated host
    # prefixes). Empty = Layer-2 installs fail closed.
    marketplace_l2_registries: str = Field(
        default="docker.io,ghcr.io", validation_alias="VELARIS_CASE_MARKETPLACE_L2_REGISTRIES")
    # Require a cosign signature on Layer-2 images (needs the cosign binary).
    marketplace_l2_require_signature: bool = Field(
        default=False, validation_alias="VELARIS_CASE_MARKETPLACE_L2_REQUIRE_SIGNATURE")
    # Resource caps for production app containers (sandbox caps stay in sandbox.py).
    marketplace_l2_mem_limit: str = Field(
        default="512m", validation_alias="VELARIS_CASE_MARKETPLACE_L2_MEM_LIMIT")
    marketplace_l2_cpu_quota: int = Field(
        default=50000, validation_alias="VELARIS_CASE_MARKETPLACE_L2_CPU_QUOTA")  # 50% of one core
    # Egress host-filter: OFF = container domain grants refused fail-closed
    # (pre-egress behavior); ON = granted domains reachable via the egress
    # gateway only. Dir override points at a non-standard egress-gw folder.
    marketplace_l2_egress_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_MARKETPLACE_L2_EGRESS")
    marketplace_l2_egress_dir: str = Field(
        default="", validation_alias="VELARIS_CASE_MARKETPLACE_L2_EGRESS_DIR")
    # <<< Marketplace Layer-2
    # AES key for encrypting source tokens and licence keys at rest (32-byte hex).
    # Falls back to storage_master_key when empty.
    # Generate: openssl rand -hex 32
    marketplace_token_key: str = ""
    # Days without a successful source sync before the admin alert fires.
    marketplace_source_stale_days: int = 7
    # <<< Marketplace

    # HxDBManager Security is now version-gated via the Release Scheduler.
    # Use is_feature_enabled("hxdbmanager_security", "v1.0.0") in code.
    # <<< HxDBManager Security

    # >>> Group I: thin webhook events + external audit anchoring
    # Webhook payloads carry IDs + status only; consumers fetch fresh case
    # variables via the API (which enforces auth). True embeds full variables
    # in outbox rows — legacy consumers only.
    webhook_full_payloads: bool = False
    # RFC-3161 anchoring: a TSA signs the audit-chain tip daily, making the
    # chain externally provable, not just internally tamper-evident.
    audit_anchor_enabled: bool = True
    audit_tsa_url: str = "https://freetsa.org/tsr"
    audit_anchor_interval_hours: int = 24
    # <<< Group I

    # >>> Group J: WebAuthn / passkeys
    # rp_id must be the registrable domain the app is served from; origin must
    # match the browser's origin exactly or every ceremony fails verification.
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Velaris"
    webauthn_origin: str = "http://localhost:5173"
    # <<< Group J

    # >>> HxCheckout (marketplace app `velaris/hxcheckout`)
    # No `enabled` flag — availability is the marketplace install (the D2 gate,
    # like HxTest); there is no bespoke on/off switch. New vars use the
    # VELARIS_CASE_ alias prefix (the class default prefix stays VELARIS_CASE_).
    checkout_default_currency: str = Field(
        default="GBP", validation_alias="VELARIS_CASE_CHECKOUT_DEFAULT_CURRENCY")
    # stripe → inline payment via the existing HxConnect Stripe connector;
    # none → invoice / cash-on-collection (payment_url is null, order skips to fulfilment).
    checkout_payment_provider: str = Field(
        default="stripe", validation_alias="VELARIS_CASE_CHECKOUT_PAYMENT_PROVIDER")
    # Auto-set to the seeded Order case_type id on first install; override to use a
    # custom case type. Empty → resolve/seed the built-in Order template on demand.
    checkout_order_case_type_id: str = Field(
        default="", validation_alias="VELARIS_CASE_CHECKOUT_ORDER_CASE_TYPE_ID")
    # Webhook HMAC: requests whose signed timestamp is older than this are rejected (replay guard).
    checkout_hmac_clock_skew_seconds: int = Field(
        default=300, validation_alias="VELARIS_CASE_CHECKOUT_HMAC_CLOCK_SKEW_SECONDS")
    # Orders per minute per service token before auto-suspend (mass-order-spam guard).
    checkout_token_rate_limit: int = Field(
        default=100, validation_alias="VELARIS_CASE_CHECKOUT_TOKEN_RATE_LIMIT")
    # Test-mode orders auto-purged after this many days (purge cron deferred post-v1).
    checkout_test_order_purge_days: int = Field(
        default=30, validation_alias="VELARIS_CASE_CHECKOUT_TEST_ORDER_PURGE_DAYS")
    # <<< HxCheckout

    # >>> HxStorefront (marketplace app `velaris/hxstorefront`)
    # No `enabled` flag — availability is the marketplace install (D2 gate, like HxTest).
    storefront_default_currency: str = Field(
        default="GBP", validation_alias="VELARIS_CASE_STOREFRONT_DEFAULT_CURRENCY")
    storefront_max_stores_per_tenant: int = Field(
        default=5, validation_alias="VELARIS_CASE_STOREFRONT_MAX_STORES_PER_TENANT")
    storefront_max_products_per_store: int = Field(
        default=10000, validation_alias="VELARIS_CASE_STOREFRONT_MAX_PRODUCTS_PER_STORE")
    storefront_max_images_per_product: int = Field(
        default=20, validation_alias="VELARIS_CASE_STOREFRONT_MAX_IMAGES_PER_PRODUCT")
    storefront_image_max_mb: int = Field(
        default=10, validation_alias="VELARIS_CASE_STOREFRONT_IMAGE_MAX_MB")
    storefront_cdn_url: str = Field(
        default="", validation_alias="VELARIS_CASE_STOREFRONT_CDN_URL")
    storefront_basket_abandonment_hours: int = Field(
        default=24, validation_alias="VELARIS_CASE_STOREFRONT_BASKET_ABANDONMENT_HOURS")
    # Custom domains + SSL provisioning are DEFERRED post-v1 (the doc's certbot flow).
    storefront_custom_domains_enabled: bool = Field(
        default=False, validation_alias="VELARIS_CASE_STOREFRONT_CUSTOM_DOMAINS_ENABLED")
    # <<< HxStorefront

    # ── R1: no single line in .env stops a service from starting (2026-10-30) ──
    #
    # `KEY=` (or a typo) on any number or on/off setting failed validation and
    # the service did not start: 106 of them, found by trying every setting with
    # empty, blank, malformed, huge and negative values. The 2026-08-24 outage
    # was the same fault in nginx. One rule here, for every field by type:
    #
    #   number, on/off   empty or blank = the default, exactly as if unset;
    #                    unreadable, negative or absurd = the default, and one
    #                    line saying which variable and what was used.
    #   a mode or place  (_DEFAULT_WHEN_EMPTY) empty = the default — for these
    #                    an empty value is never a meaningful choice.
    #   case enforcement anything but enforce / shadow / off = enforce. Empty
    #                    read as "shadow": every case gate advisory, the open
    #                    state its default exists to prevent.
    #   refused          (_REFUSE_WHEN_EMPTY) the service stops and names the
    #                    variable: an empty signing secret is a key anyone can
    #                    sign with, and the vault key is derived from it.
    #
    # Text settings are otherwise taken as written: for many, empty is how a
    # feature is turned off, and a secret is never trimmed.
    @field_validator("*", mode="before")
    @classmethod
    def _never_stop_a_boot(cls, value, info):
        field = cls.model_fields[info.field_name]
        kind = _KIND.get(field.annotation)
        name = info.field_name
        if kind is None:
            if not isinstance(value, str) or value.strip():
                return value
            if name in _REFUSE_WHEN_EMPTY:
                raise ValueError(
                    f"{_env_name(name, field)} is empty — refusing to start: {_REFUSE_WHEN_EMPTY[name]}")
            if name in _EMPTY_DECIDED_LATER:
                return ""
            if name in _DEFAULT_WHEN_EMPTY:
                return field.default
            return value
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return field.default
        try:
            parsed = _ADAPTERS[kind].validate_python(text)
            if kind is not bool and parsed is not None and not (0 <= parsed <= _ABSURD):
                raise ValueError("out of range")
            return parsed
        except ValueError:
            _log.warning("%s=%r is not a valid %s — using the default (%r)",
                         _env_name(name, field), text[:40], _KIND_WORD[kind], field.default)
            return field.default

    @field_validator("hxguard_case_enforcement")
    @classmethod
    def _enforce_unless_said_otherwise(cls, value):
        mode = (value or "").strip().lower()
        if mode in ("enforce", "shadow", "off"):
            return mode
        if mode:
            _log.warning("VELARIS_CASE_HXGUARD_CASE_ENFORCEMENT=%r is not enforce, shadow "
                         "or off — enforcing", value[:40])
        return "enforce"

    @model_validator(mode="after")
    def _empty_database_url(self) -> "Settings":
        # Empty URL = the backend builds it from the typed components. That needs
        # at least a host — or a MySQL-family install, whose setup always writes
        # the components and whose builder has always defaulted the rest. An
        # empty URL on PostgreSQL with no host is the half-edited .env R1 exists
        # to stop: refused, and named.
        if self.database_url == "" and not self.db_host \
                and (self.database_backend or "postgresql") == "postgresql":
            raise ValueError(
                "VELARIS_CASE_DATABASE_URL is empty and VELARIS_CASE_DB_HOST is not set — refusing to "
                "start: set the URL, or the DB_HOST/_PORT/_NAME/_USER components, or remove the line "
                "to use the default")
        return self

    @model_validator(mode="after")
    def _fix_rsa_newlines(self) -> "Settings":
        # .env files store \n as literal two-char sequence; convert to real newlines
        if self.auth_rsa_private_key:
            self.auth_rsa_private_key = self.auth_rsa_private_key.replace("\\n", "\n")
        if self.auth_rsa_public_key:
            self.auth_rsa_public_key = self.auth_rsa_public_key.replace("\\n", "\n")
        return self

    model_config = {"env_prefix": "VELARIS_CASE_", "env_file": ".env", "extra": "ignore"}


_KIND = {int: int, float: float, bool: bool, int | None: int}
_KIND_WORD = {int: "whole number", float: "number", bool: "on/off value"}
_ADAPTERS = {k: TypeAdapter(k) for k in (int, float, bool)}
#: Beyond any real limit, timeout or size: a typo with extra digits.
_ABSURD = 2 ** 53

_DEFAULT_WHEN_EMPTY = frozenset({
    "service_name", "service_host", "database_backend", "temporal_host",
    "temporal_namespace", "temporal_task_queue", "engine_api_url", "auth_mode",
    "auth_issuer", "auth_audience", "internal_tls_dir", "storage_backend",
    "storage_local_path", "redis_url", "redis_channel_prefix", "rate_limit_backend",
    "ai_egress_policy", "event_bus_topic_prefix", "alert_smtp_tls", "ocr_language",
    "webauthn_rp_name", "checkout_default_currency", "storefront_default_currency",
    "marketplace_conformance_gate", "meet_driver", "meet_asr_backend",
})
_REFUSE_WHEN_EMPTY = {
    "auth_secret": "tokens would be signed with an empty key, and the vault key is "
                   "derived from it; set it back to this install's secret",
}
#: Empty is a documented setting for these, decided with the whole model in view
#: (Settings._empty_database_url): an empty VELARIS_CASE_DATABASE_URL means
#: "build it from VELARIS_CASE_DB_HOST/_PORT/_NAME/_USER" — what every MySQL and
#: MariaDB install's setup writes. Refusing it outright (R1, 2026-10-30) stopped
#: every such install from starting on upgrade (R3 re-run, 2026-11-01).
_EMPTY_DECIDED_LATER = frozenset({"database_url"})


def env_int(name: str, default: int) -> int:
    """A whole number read straight from the environment, outside `Settings`, by
    the same R1 rule: unset, empty or unreadable is the default (the last with a
    line saying so) — never a crash at import, which stops the whole service."""
    import os

    text = (os.environ.get(name) or "").strip()
    if not text:
        return default
    try:
        n = int(text)
        if 0 <= n <= _ABSURD:
            return n
    except ValueError:
        pass
    _log.warning("%s=%r is not a valid whole number — using the default (%r)", name, text[:40], default)
    return default


def _env_name(name: str, field) -> str:
    alias = field.validation_alias
    if isinstance(alias, str):
        return alias
    if isinstance(alias, AliasChoices):
        return next((c for c in alias.choices if isinstance(c, str)), name)
    return "VELARIS_CASE_" + name.upper()


@lru_cache
def get_settings() -> Settings:
    return Settings()
