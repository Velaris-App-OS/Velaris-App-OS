-- HxDBMigrate P4 (Batch Data Migration): a record of each source-table → cases run.

CREATE TABLE IF NOT EXISTS hxdbmigrate_migration_runs (
    id                UUID PRIMARY KEY,
    source_id         UUID         NOT NULL,
    tenant_id         VARCHAR(255),
    table_name        VARCHAR(255) NOT NULL,
    case_type_id      UUID,
    status            VARCHAR(32)  NOT NULL DEFAULT 'complete',  -- complete | failed | dry_run
    pii_mode          VARCHAR(16)  NOT NULL DEFAULT 'safe',      -- safe | exclude_all | as_is
    dry_run           BOOLEAN      NOT NULL DEFAULT FALSE,
    rows_read         INTEGER,
    rows_migrated     INTEGER,
    excluded_columns  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    error             TEXT,
    created_by        VARCHAR(255),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_run_source  ON hxdbmigrate_migration_runs (source_id);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_run_tenant  ON hxdbmigrate_migration_runs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_run_created ON hxdbmigrate_migration_runs (created_at);
