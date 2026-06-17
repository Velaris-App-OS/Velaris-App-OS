"""Case-service configuration via environment variables.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Case-service settings loaded from env / .env file."""

    # Service
    service_name: str = "helix-case-service"
    service_host: str = "0.0.0.0"
    service_port: int = 8200
    debug: bool = False

    # Database (async PostgreSQL)
    database_url: str = (
        "postgresql+asyncpg://helix:helix_dev_password@localhost:5432/helix"
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
    storage_local_path: str = "./data/helix-docs"  # relative to working dir; override via HELIX_CASE_STORAGE_LOCAL_PATH
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

    # >>> HxGuard Phase B (env: HELIX_CASE_HXGUARD_CASE_ENFORCEMENT)
    # off    = case-level ReBAC checks skipped entirely
    # shadow = evaluated + would-be denials audited, requests pass (default)
    # enforce = denials return 403
    hxguard_case_enforcement: str = "shadow"
    # <<< HxGuard Phase B

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
