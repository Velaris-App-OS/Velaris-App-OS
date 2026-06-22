-- 094: Marketplace schema — creates the 12 marketplace_* tables.
-- These tables back the Marketplace feature (sources, package cache, sandbox
-- workspaces, installs, network log, whitelist, updates, blacklist, access
-- rules, release requests, sandbox datasets). They were defined as ORM models
-- but never had a PostgreSQL migration, so on Postgres the tables never existed
-- and any authenticated /api/v1/marketplace/* call 500'd on a missing relation.
-- Idempotent (IF NOT EXISTS). marketplace_workspaces is created WITH the
-- conformance columns, so 092 stays a guarded no-op ahead of this file.
-- Column types/nullability/defaults/indexes mirror db/models.py exactly.

CREATE TABLE IF NOT EXISTS marketplace_sources (
    id                   UUID         PRIMARY KEY,
    name                 VARCHAR(255) NOT NULL,
    url                  VARCHAR(1024) NOT NULL,
    tier                 VARCHAR(32)  NOT NULL DEFAULT 'community',
    token_enc            TEXT         NULL,
    poll_interval_hours  INTEGER      NOT NULL DEFAULT 6,
    enabled              BOOLEAN      NOT NULL DEFAULT TRUE,
    last_polled_at       TIMESTAMPTZ  NULL,
    last_error           TEXT         NULL,
    package_count        INTEGER      NOT NULL DEFAULT 0,
    added_by             VARCHAR(255) NOT NULL,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_mks_url UNIQUE (url)
);
CREATE INDEX IF NOT EXISTS ix_mks_tier ON marketplace_sources (tier);

CREATE TABLE IF NOT EXISTS marketplace_packages (
    id                   VARCHAR(255) PRIMARY KEY,
    name                 VARCHAR(255) NOT NULL,
    description          TEXT         NOT NULL,
    package_type         VARCHAR(64)  NOT NULL,
    category             VARCHAR(128) NOT NULL,
    publisher            VARCHAR(255) NOT NULL,
    publisher_tier       VARCHAR(32)  NOT NULL,
    version              VARCHAR(64)  NOT NULL,
    price                VARCHAR(32)  NOT NULL,
    price_label          VARCHAR(64)  NULL,
    contact_url          VARCHAR(512) NULL,
    rating               DOUBLE PRECISION NOT NULL DEFAULT 0,
    installs             INTEGER      NOT NULL DEFAULT 0,
    download_url         VARCHAR(512) NOT NULL,
    checksum_sha256      VARCHAR(64)  NOT NULL,
    outbound_domains     TEXT         NOT NULL DEFAULT '[]',
    tags                 TEXT         NOT NULL DEFAULT '[]',
    icon_color           VARCHAR(16)  NULL,
    icon_letter          VARCHAR(8)   NULL,
    min_platform_version VARCHAR(32)  NOT NULL DEFAULT '1.0.0',
    updated_at           VARCHAR(32)  NULL,
    release_notes        TEXT         NULL,
    all_versions         TEXT         NOT NULL DEFAULT '[]',
    source_id            UUID         NULL REFERENCES marketplace_sources (id) ON DELETE SET NULL,
    fetched_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS marketplace_workspaces (
    id                     UUID         PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL,
    name                   VARCHAR(255) NOT NULL,
    status                 VARCHAR(32)  NOT NULL DEFAULT 'active',
    dataset_id             UUID         NULL,
    container_id           VARCHAR(255) NULL,
    created_by             VARCHAR(255) NOT NULL,
    reviewed_by            VARCHAR(255) NULL,
    review_note            TEXT         NULL,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at             TIMESTAMPTZ  NOT NULL,
    submitted_at           TIMESTAMPTZ  NULL,
    reviewed_at            TIMESTAMPTZ  NULL,
    conformance_status     VARCHAR(30)  NOT NULL DEFAULT 'none',
    conformance_run_id     UUID         NULL,
    conformance_checked_at TIMESTAMPTZ  NULL
);
CREATE INDEX IF NOT EXISTS ix_mw_tenant ON marketplace_workspaces (tenant_id);
CREATE INDEX IF NOT EXISTS ix_mw_user   ON marketplace_workspaces (created_by);
CREATE INDEX IF NOT EXISTS ix_mw_status ON marketplace_workspaces (status);

CREATE TABLE IF NOT EXISTS marketplace_workspace_items (
    id              UUID         PRIMARY KEY,
    workspace_id    UUID         NOT NULL REFERENCES marketplace_workspaces (id) ON DELETE CASCADE,
    package_id      VARCHAR(255) NOT NULL,
    package_version VARCHAR(64)  NOT NULL,
    status          VARCHAR(32)  NOT NULL DEFAULT 'installed',
    licence_key_enc TEXT         NULL,
    installed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    approved_at     TIMESTAMPTZ  NULL
);
CREATE INDEX IF NOT EXISTS ix_mwi_workspace ON marketplace_workspace_items (workspace_id);

CREATE TABLE IF NOT EXISTS marketplace_installs (
    id              UUID         PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    package_id      VARCHAR(255) NOT NULL,
    package_version VARCHAR(64)  NOT NULL,
    package_type    VARCHAR(64)  NOT NULL,
    licence_key_enc TEXT         NULL,
    licence_expires VARCHAR(32)  NULL,
    approved_by     VARCHAR(255) NOT NULL,
    workspace_id    UUID         NULL,
    installed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ  NULL,
    CONSTRAINT uq_mi_tenant_package UNIQUE (tenant_id, package_id)
);
CREATE INDEX IF NOT EXISTS ix_mi_tenant ON marketplace_installs (tenant_id);

CREATE TABLE IF NOT EXISTS marketplace_network_log (
    id               UUID         PRIMARY KEY,
    workspace_id     UUID         NOT NULL REFERENCES marketplace_workspaces (id) ON DELETE CASCADE,
    package_id       VARCHAR(255) NOT NULL,
    destination_url  VARCHAR(1024) NOT NULL,
    destination_ip   VARCHAR(64)  NULL,
    http_method      VARCHAR(16)  NULL,
    bytes_sent       INTEGER      NOT NULL DEFAULT 0,
    bytes_received   INTEGER      NOT NULL DEFAULT 0,
    status           VARCHAR(16)  NOT NULL,
    http_status_code INTEGER      NULL,
    is_declared      BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mnl_workspace ON marketplace_network_log (workspace_id);
CREATE INDEX IF NOT EXISTS ix_mnl_package   ON marketplace_network_log (package_id);

CREATE TABLE IF NOT EXISTS marketplace_whitelist (
    id            UUID         PRIMARY KEY,
    workspace_id  UUID         NOT NULL REFERENCES marketplace_workspaces (id) ON DELETE CASCADE,
    package_id    VARCHAR(255) NOT NULL,
    domain        VARCHAR(255) NOT NULL,
    justification TEXT         NULL,
    status        VARCHAR(32)  NOT NULL DEFAULT 'pending',
    requested_by  VARCHAR(255) NOT NULL,
    decided_by    VARCHAR(255) NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    decided_at    TIMESTAMPTZ  NULL
);
CREATE INDEX IF NOT EXISTS ix_mwl_workspace ON marketplace_whitelist (workspace_id);

CREATE TABLE IF NOT EXISTS marketplace_updates (
    id                   UUID         PRIMARY KEY,
    tenant_id            VARCHAR(255) NOT NULL,
    package_id           VARCHAR(255) NOT NULL,
    installed_version    VARCHAR(64)  NOT NULL,
    available_version    VARCHAR(64)  NOT NULL,
    release_notes        TEXT         NULL,
    new_outbound_domains TEXT         NOT NULL DEFAULT '[]',
    fast_track           BOOLEAN      NOT NULL DEFAULT TRUE,
    status               VARCHAR(32)  NOT NULL DEFAULT 'pending',
    detected_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    approved_at          TIMESTAMPTZ  NULL,
    approved_by          VARCHAR(255) NULL,
    CONSTRAINT uq_mku_tenant_package UNIQUE (tenant_id, package_id)
);
CREATE INDEX IF NOT EXISTS ix_mku_tenant ON marketplace_updates (tenant_id);

CREATE TABLE IF NOT EXISTS marketplace_blacklist (
    id             UUID         PRIMARY KEY,
    tenant_id      VARCHAR(255) NOT NULL,
    type           VARCHAR(32)  NOT NULL,
    value          VARCHAR(1024) NOT NULL,
    reason         TEXT         NOT NULL,
    blacklisted_by VARCHAR(255) NOT NULL,
    notify_velaris BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mbl_tenant     ON marketplace_blacklist (tenant_id);
CREATE INDEX IF NOT EXISTS ix_mbl_type_value ON marketplace_blacklist (type, value);

CREATE TABLE IF NOT EXISTS marketplace_access_rules (
    id                  UUID         PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL,
    access_group_id     VARCHAR(255) NOT NULL,
    rule_type           VARCHAR(32)  NOT NULL DEFAULT 'allow_all',
    allowed_package_ids TEXT         NOT NULL DEFAULT '[]',
    blocked_package_ids TEXT         NOT NULL DEFAULT '[]',
    updated_by          VARCHAR(255) NOT NULL DEFAULT 'system',
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_mar_tenant_group UNIQUE (tenant_id, access_group_id)
);
CREATE INDEX IF NOT EXISTS ix_mar_tenant ON marketplace_access_rules (tenant_id);

CREATE TABLE IF NOT EXISTS marketplace_release_requests (
    id                    UUID         PRIMARY KEY,
    tenant_id             VARCHAR(255) NOT NULL,
    package_id            VARCHAR(255) NOT NULL,
    package_version       VARCHAR(64)  NOT NULL,
    requested_by          VARCHAR(255) NOT NULL,
    status                VARCHAR(32)  NOT NULL DEFAULT 'pending',
    included_in_deploy_id VARCHAR(255) NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deployed_at           TIMESTAMPTZ  NULL,
    CONSTRAINT uq_mrr_tenant_package_status UNIQUE (tenant_id, package_id, status)
);
CREATE INDEX IF NOT EXISTS ix_mrr_tenant ON marketplace_release_requests (tenant_id);

CREATE TABLE IF NOT EXISTS marketplace_sandbox_datasets (
    id          UUID         PRIMARY KEY,
    tenant_id   VARCHAR(255) NOT NULL,
    name        VARCHAR(255) NOT NULL,
    description TEXT         NULL,
    data_json   TEXT         NOT NULL DEFAULT '{}',
    created_by  VARCHAR(255) NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
