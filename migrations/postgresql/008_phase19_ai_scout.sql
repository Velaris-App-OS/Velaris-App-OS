-- HELIX Phase 19 Migration: AI artifact analysis results
BEGIN;

CREATE TABLE IF NOT EXISTS artifact_analyses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scan_id             UUID REFERENCES migration_scans(id) ON DELETE CASCADE,
    artifact_identifier VARCHAR(500) NOT NULL,
    artifact_type       VARCHAR(100),
    source_code         TEXT,
    summary             TEXT,
    business_logic      TEXT,
    complexity          VARCHAR(20),
    external_calls      JSONB DEFAULT '[]',
    data_reads          JSONB DEFAULT '[]',
    data_writes         JSONB DEFAULT '[]',
    side_effects        JSONB DEFAULT '[]',
    helix_mapping       JSONB DEFAULT '{}',
    generated_code      TEXT,
    confidence          FLOAT,
    ai_model            VARCHAR(100),
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifact_analyses_scan ON artifact_analyses(scan_id);
CREATE INDEX IF NOT EXISTS idx_artifact_analyses_complexity ON artifact_analyses(complexity);

COMMIT;
