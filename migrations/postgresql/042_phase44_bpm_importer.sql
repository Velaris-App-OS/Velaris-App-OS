-- P44: BPM App Importer
-- import_jobs: tracks a five-pass import pipeline for Pega/Camunda/Appian/ServiceNow exports
--
-- Passes: extract → parse → map → generate → report

BEGIN;

CREATE TABLE import_jobs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tool            VARCHAR(50) NOT NULL,          -- pega | camunda | appian | servicenow
    filename        VARCHAR(500) NOT NULL,
    status          VARCHAR(30) NOT NULL DEFAULT 'pending',
                                                   -- pending|extracting|parsing|mapping|
                                                   -- generating|complete|failed
    pass1_result    JSONB       NOT NULL DEFAULT '{}',   -- extracted file manifest
    pass2_result    JSONB       NOT NULL DEFAULT '{}',   -- parsed rules
    pass3_result    JSONB       NOT NULL DEFAULT '{}',   -- mapped rules → Helix equivalents
    pass4_result    JSONB       NOT NULL DEFAULT '{}',   -- generated Helix objects
    report          JSONB       NOT NULL DEFAULT '{}',   -- final import report
    error           TEXT,
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX ix_import_jobs_status  ON import_jobs(status);
CREATE INDEX ix_import_jobs_tool    ON import_jobs(tool);
CREATE INDEX ix_import_jobs_created ON import_jobs(created_at DESC);

COMMIT;
