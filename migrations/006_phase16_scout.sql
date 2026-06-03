-- HELIX Phase 16 Migration: Scout migration scanner
BEGIN;

CREATE TABLE IF NOT EXISTS migration_scans (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    source_platform VARCHAR(50) NOT NULL,     -- "pega", "appian", "camunda", "servicenow"
    source_version  VARCHAR(100),
    filename        VARCHAR(500),
    status          VARCHAR(30) DEFAULT 'pending',  -- pending, scanning, completed, failed
    compatibility_score FLOAT,                 -- 0.0-1.0
    effort_weeks    INT,
    artifacts_found JSONB DEFAULT '{}',        -- counts by type
    scan_report     JSONB DEFAULT '{}',        -- full analysis
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_migration_scans_status
    ON migration_scans(status);
CREATE INDEX IF NOT EXISTS idx_migration_scans_platform
    ON migration_scans(source_platform);

COMMIT;
