"""Case-service configuration via environment variables.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from functools import lru_cache

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
    db_pool_size: int = 10
    db_max_overflow: int = 5

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
    token_expiry_days: int = 60       # default; overridden by helix_settings DB table
    oidc_discovery_url: str = ""
    oidc_client_id: str = "helix-studio"

    # RS256 asymmetric JWT (recommended for production).
    # When both are set, RS256 is used for all token operations and auth_secret is ignored.
    # Generate with: python -c "from case_service.auth.jwt_handler import generate_rsa_keypair; generate_rsa_keypair()"
    # Set HELIX_CASE_AUTH_RSA_PRIVATE_KEY and HELIX_CASE_AUTH_RSA_PUBLIC_KEY to the PEM strings.
    # Newlines in env vars must be encoded as literal \n  (most .env loaders handle this).
    auth_rsa_private_key: str = ""  # PEM-encoded RSA-2048 private key (signing only)
    auth_rsa_public_key: str = ""   # PEM-encoded RSA-2048 public key  (verification only)

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
    marketplace_index_url: str = (
        "https://raw.githubusercontent.com/velaris-marketplace/index/main/sources.json"
    )
    # GitHub org (or orgs) whose source URLs are treated as Official tier.
    # Comma-separated. Any source URL not matching these is always Community.
    # GitHub org names (comma-separated) whose source URLs are Official.
    # Just the org name — works for both github.com and raw.githubusercontent.com URLs.
    marketplace_official_orgs: str = "velaris-marketplace"
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

    model_config = {"env_prefix": "HELIX_CASE_", "env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
