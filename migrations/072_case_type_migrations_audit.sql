-- Migration 072: case_type_migrations — HxMigrate import audit trail
-- Every case type imported via HxMigrate gets a record here.
-- Visible to all users; immutable after creation.

CREATE TABLE IF NOT EXISTS case_type_migrations (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    case_type_id        UUID        NOT NULL,
    run_id              UUID        NULL,
    source_platform     VARCHAR(100) NOT NULL DEFAULT '',
    source_filename     VARCHAR(500) NOT NULL DEFAULT '',
    imported_by_user_id TEXT        NOT NULL DEFAULT '',
    imported_by_email   TEXT        NOT NULL DEFAULT '',
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stages_count        INT         NOT NULL DEFAULT 0,
    steps_count         INT         NOT NULL DEFAULT 0,
    forms_count         INT         NOT NULL DEFAULT 0,
    rules_count         INT         NOT NULL DEFAULT 0,
    slas_count          INT         NOT NULL DEFAULT 0,
    notes               TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ctm_case_type_id ON case_type_migrations(case_type_id);
CREATE INDEX IF NOT EXISTS idx_ctm_imported_at  ON case_type_migrations(imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_ctm_run_id       ON case_type_migrations(run_id) WHERE run_id IS NOT NULL;
