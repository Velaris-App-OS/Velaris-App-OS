-- HxDBMigrate P1 (Connect + Discover): external source registry + discovery analyses.
-- Credentials are stored HxVault-encrypted in the JSONB column (never plaintext).

CREATE TABLE IF NOT EXISTS hxdbmigrate_sources (
    id                 UUID PRIMARY KEY,
    name               VARCHAR(255) NOT NULL,
    source_type        VARCHAR(32)  NOT NULL,   -- postgresql | mysql | mariadb (allowlist)
    host               VARCHAR(255) NOT NULL,
    port               INTEGER      NOT NULL,
    database           VARCHAR(255) NOT NULL,
    username           VARCHAR(255) NOT NULL,
    ssl_mode           VARCHAR(16)  NOT NULL DEFAULT 'disable',  -- disable | require | verify
    credentials        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    tenant_id          VARCHAR(255),
    last_connected_at  TIMESTAMPTZ,
    last_connect_ok    BOOLEAN,
    created_by         VARCHAR(255),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hxdbmig_src_name_tenant UNIQUE (name, tenant_id)
);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_src_tenant ON hxdbmigrate_sources (tenant_id);

CREATE TABLE IF NOT EXISTS hxdbmigrate_analyses (
    id             UUID PRIMARY KEY,
    source_id      UUID         NOT NULL,
    tenant_id      VARCHAR(255),
    status         VARCHAR(32)  NOT NULL DEFAULT 'complete',  -- complete | failed
    table_count    INTEGER,
    quality_score  INTEGER,
    report         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    error          TEXT,
    created_by     VARCHAR(255),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_an_source  ON hxdbmigrate_analyses (source_id);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_an_tenant  ON hxdbmigrate_analyses (tenant_id);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_an_created ON hxdbmigrate_analyses (created_at);
