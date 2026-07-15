-- HELIX Phase 21 Migration: Migration Orchestrator
BEGIN;

CREATE TABLE IF NOT EXISTS migration_projects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    source_platform VARCHAR(50),
    scan_id         UUID REFERENCES migration_scans(id) ON DELETE SET NULL,
    status          VARCHAR(30) DEFAULT 'draft',
    -- draft, analyzing, ready, in_progress, completed, failed
    total_artifacts INT DEFAULT 0,
    analyzed_count  INT DEFAULT 0,
    generated_count INT DEFAULT 0,
    ported_count    INT DEFAULT 0,
    roadmap         JSONB DEFAULT '{}',
    dependencies    JSONB DEFAULT '{}',
    settings        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_migration_projects_status
    ON migration_projects(status);

CREATE TABLE IF NOT EXISTS migration_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES migration_projects(id) ON DELETE CASCADE,
    artifact_id     VARCHAR(500) NOT NULL,
    artifact_type   VARCHAR(100),
    artifact_name   VARCHAR(255),
    phase           INT DEFAULT 1,                   -- 1=quick wins, 2=high compat, 3=medium, 4=complex
    sequence        INT DEFAULT 0,                   -- ordering within phase based on deps
    status          VARCHAR(30) DEFAULT 'pending',
    -- pending, analyzing, ready, generating, generated, ported, skipped, failed
    depends_on      JSONB DEFAULT '[]',
    analysis_id     UUID REFERENCES artifact_analyses(id) ON DELETE SET NULL,
    generated_code  TEXT,
    complexity      VARCHAR(20),
    estimated_hours FLOAT,
    actual_hours    FLOAT,
    notes           TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_migration_tasks_project
    ON migration_tasks(project_id, phase, sequence);
CREATE INDEX IF NOT EXISTS idx_migration_tasks_status
    ON migration_tasks(status);

COMMIT;
