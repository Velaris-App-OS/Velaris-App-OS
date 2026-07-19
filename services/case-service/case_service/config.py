"""Case-service configuration via environment variables.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Case-service settings loaded from env / .env file."""

    # Service
    service_name: str = "helix-case-service"
    service_host: str = "0.0.0.0"
    service_port: int = 8200
    debug: bool = False

    # Database (DB SDK — multi-dialect). `database_backend` selects a FIRST-PARTY backend
    # from the baked-in allowlist (case_service.db.backends.ALLOWED_BACKENDS); an unknown
    # value aborts startup. Set once by start-velaris.sh from `velaris.yaml: database:`.
    database_backend: str = Field(
        default="postgresql",
        validation_alias=AliasChoices("VELARIS_CASE_DATABASE_BACKEND", "DATABASE_BACKEND"),
    )
    # Full URL takes precedence when set (current behaviour; trusted operator / OpenBao
    # config). Empty → the backend builds the URL from the typed components below.
    database_url: str = (
        "postgresql+asyncpg://helix:helix_dev_password@localhost:5432/helix"
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
    # Optional READ-ONLY replica (hot standby) for heavy analytics queries.
    # Empty = those queries share the analytics pool on the primary. Served
    # via get_replica_session only — the analytics pool itself stays on the
    # primary because compliance seals, hxsync, PUO, and saved-report CRUD
    # write through it and would fail on a standby.
    db_analytics_url: str = ""

    # D2: request body size limits (three tiers)
    max_body_bytes: int = 10 * 1024 * 1024             # JSON payloads
    max_upload_bytes: int = 25 * 1024 * 1024           # multipart uploads (docs, portal, etc.)
    max_migrate_upload_bytes: int = 200 * 1024 * 1024  # hxmigrate BPM exports only

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "helix-case-queue"

    # Engine API (for deploying lifecycle processes)
    engine_api_url: str = "http://localhost:8100"

    # NLP settings
    nlp_enabled: bool = True
    nlp_fallback_enabled: bool = True  # Use heuristic fallback if LLM unavailable

    # Auth
    # IMPORTANT: auth_secret MUST be set via HELIX_CASE_AUTH_SECRET in production.
    # The default is intentionally weak and prints a startup warning if unchanged.
    auth_mode: str = "jwt"  # "jwt" | "oidc"
    auth_secret: str = "helix-dev-secret-change-in-production"
    auth_issuer: str = "helix"
    auth_audience: str = "helix-api"
    token_expiry_days: int = 60             # refresh token lifetime; overridden by helix_settings DB table
    access_token_expiry_minutes: int = 15   # short-lived access token (override via HELIX_CASE_ACCESS_TOKEN_EXPIRY_MINUTES)
    oidc_discovery_url: str = ""
    oidc_client_id: str = "helix-studio"

    # RS256 asymmetric JWT (recommended for production).
    # When both are set, RS256 is used for all token operations and auth_secret is ignored.
    # Generate with: python -c "from case_service.auth.jwt_handler import generate_rsa_keypair; generate_rsa_keypair()"
    # Set HELIX_CASE_AUTH_RSA_PRIVATE_KEY and HELIX_CASE_AUTH_RSA_PUBLIC_KEY to the PEM strings.
    # Newlines in env vars must be encoded as literal \n  (most .env loaders handle this).
    auth_rsa_private_key: str = ""  # PEM-encoded RSA-2048 private key (signing only)
    auth_rsa_public_key: str = ""   # PEM-encoded RSA-2048 public key  (verification only)

    # HxVault (#19) — master Key-Encryption-Key that wraps per-tenant DEKs.
    # 32 bytes, hex (64 chars) or base64. MUST be set via VELARIS_CASE_KEK in
    # production (rendered from OpenBao). New env vars use the VELARIS_CASE_ prefix
    # (field-level alias; the class default prefix is still HELIX_CASE_ for legacy
    # vars). When empty, a dev KEK is derived from auth_secret (round-trips fine;
    # crypto-shred still works since DEKs are random+stored) + startup warning.
    kek: str = Field(default="", validation_alias="VELARIS_CASE_KEK")
    # HxVault multi-worker coherence: how often each worker reconciles its in-process
    # DEK cache with the DB (picks up DEKs created on other workers, evicts shredded
    # ones). Lower = faster cross-worker propagation, more DB reads. 0 disables.
    vault_cache_resync_seconds: int = Field(default=30, validation_alias="VELARIS_CASE_VAULT_RESYNC_SECONDS")

    # >>> P24 document storage
    storage_backend: str = "local"  # "local" | "minio"
    storage_local_path: str = Field(
        default="./data/helix-docs",  # relative to working dir
        # New installs use VELARIS_CASE_STORAGE_LOCAL_PATH; HELIX_CASE_ kept for legacy .env files.
        validation_alias=AliasChoices(
            "VELARIS_CASE_STORAGE_LOCAL_PATH",
            "HELIX_CASE_STORAGE_LOCAL_PATH",
        ),
    )
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "helix"
    minio_secret_key: str = "helix_dev_password"
    minio_bucket: str = "helix-docs"
    minio_secure: bool = False

    # At-rest encryption for document storage (local and MinIO).
    # Set HELIX_CASE_STORAGE_MASTER_KEY to a 64-char hex string (32 bytes).
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

    # >>> HxGuard Phase B (env: HELIX_CASE_HXGUARD_CASE_ENFORCEMENT)
    # off    = case-level ReBAC checks skipped entirely
    # shadow = evaluated + would-be denials audited, requests pass (default)
    # enforce = denials return 403
    hxguard_case_enforcement: str = "shadow"
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
    # <<< HxNexus Operator (MCP) P1

    # >>> P32 scaling
    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    redis_channel_prefix: str = "helix:rt:"
    rate_limit_backend: str = "memory"  # "memory" | "redis"
    # <<< P32 scaling

    # >>> Unified AI backend (used by HxNexus, NLP Builder, Scout AI)
    #
    # SIMPLE (new) — set just these 3-4 vars for any provider:
    #   HELIX_CASE_AI_PROVIDER = groq | mistral | gemini | together | openai | anthropic | ollama | custom
    #   HELIX_CASE_AI_API_KEY  = your API key
    #   HELIX_CASE_AI_MODEL    = model name (e.g. llama-3.1-70b-versatile)
    #   HELIX_CASE_AI_EMBED_MODEL = embedding model (optional, falls back to ollama)
    #   HELIX_CASE_AI_BASE_URL = only needed for 'custom' or to override a known provider
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
    # VELARIS_CASE_ alias prefix (the class default prefix stays HELIX_CASE_).
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

    @model_validator(mode="after")
    def _fix_rsa_newlines(self) -> "Settings":
        # .env files store \n as literal two-char sequence; convert to real newlines
        if self.auth_rsa_private_key:
            self.auth_rsa_private_key = self.auth_rsa_private_key.replace("\\n", "\n")
        if self.auth_rsa_public_key:
            self.auth_rsa_public_key = self.auth_rsa_public_key.replace("\\n", "\n")
        return self

    model_config = {"env_prefix": "HELIX_CASE_", "env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
