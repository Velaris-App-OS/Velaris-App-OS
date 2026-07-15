-- P54 HxMigrate: Unified Migration Intelligence Pipeline

CREATE TABLE migration_pipeline_runs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    name            TEXT        NOT NULL,
    source_platform VARCHAR(50) NOT NULL,
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | running | completed | failed | partial
    mode            VARCHAR(20) NOT NULL DEFAULT 'full',
    -- full | step_by_step
    current_stage   INTEGER     NOT NULL DEFAULT 0,
    -- 0=not started, 1=scout, 2=ai_analysis, 3=generation, 4=orchestration, 5=packaging
    scan_id         UUID,
    import_job_id   UUID,
    project_id      UUID,
    package_id      UUID,
    source_filename TEXT,
    source_size     BIGINT,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX ix_mpr_tenant ON migration_pipeline_runs(tenant_id);
CREATE INDEX ix_mpr_status ON migration_pipeline_runs(status);

CREATE TABLE pipeline_stage_events (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID        NOT NULL REFERENCES migration_pipeline_runs(id) ON DELETE CASCADE,
    stage       INTEGER     NOT NULL,
    stage_name  VARCHAR(100) NOT NULL,
    status      VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | running | completed | failed | skipped
    summary     JSONB       NOT NULL DEFAULT '{}',
    error       TEXT,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX ix_pse_run ON pipeline_stage_events(run_id);
