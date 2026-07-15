-- HxDBMigrate P5/P6 (Continuous Sync + Cutover): row-link identity spine,
-- source lifecycle, and sync-run bookkeeping.

-- Source row → Velaris case identity. Makes sync idempotent (upsert by source PK)
-- and cutover reversible (rollback knows exactly which cases this source created).
CREATE TABLE IF NOT EXISTS hxdbmigrate_row_links (
    id              UUID PRIMARY KEY,
    source_id       UUID         NOT NULL,
    tenant_id       VARCHAR(255),
    table_name      VARCHAR(255) NOT NULL,
    source_pk       VARCHAR(512) NOT NULL,
    case_id         UUID         NOT NULL,
    case_type_id    UUID,
    row_checksum    VARCHAR(64),
    last_synced_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hxdbmig_link_row UNIQUE (source_id, table_name, source_pk)
);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_link_source ON hxdbmigrate_row_links (source_id);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_link_case   ON hxdbmigrate_row_links (case_id);
CREATE INDEX IF NOT EXISTS ix_hxdbmig_link_tenant ON hxdbmigrate_row_links (tenant_id);

-- Source lifecycle: active -> cutover -> completed | (rollback -> active).
ALTER TABLE hxdbmigrate_sources ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active';
ALTER TABLE hxdbmigrate_sources ADD COLUMN IF NOT EXISTS cutover_at TIMESTAMPTZ;
ALTER TABLE hxdbmigrate_sources ADD COLUMN IF NOT EXISTS rollback_window_hours INTEGER NOT NULL DEFAULT 72;

-- Runs now cover sync/cutover/rollback passes too, and count updates separately.
ALTER TABLE hxdbmigrate_migration_runs ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'migrate';
ALTER TABLE hxdbmigrate_migration_runs ADD COLUMN IF NOT EXISTS rows_updated INTEGER;
