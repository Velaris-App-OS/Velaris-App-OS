-- 091: Test Suite (core, #27) — deterministic test engine tables.
-- Stores test-suite definitions, runs, and per-test results. Powers the
-- platform smoke/component/security suites and the marketplace structural
-- conformance gate. The HxTest marketplace layer (AI generation) builds on top.

CREATE TABLE IF NOT EXISTS hxtest_suites (
    id            UUID         PRIMARY KEY,
    name          VARCHAR(200) NOT NULL,
    suite_type    VARCHAR(30)  NOT NULL,            -- platform|component|security|conformance|generated
    source        VARCHAR(20)  NOT NULL DEFAULT 'builtin',  -- builtin|ai_generated|developer
    case_type_id  UUID         NULL,                -- set when generated for a case type
    definition    JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- list of test-case defs (DSL)
    version       VARCHAR(40)  NOT NULL DEFAULT '1.0.0',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hxtest_runs (
    id                   UUID        PRIMARY KEY,
    suite_id             UUID        NULL,          -- NULL for ad-hoc / multi-suite "all"
    suite_name           VARCHAR(200),
    triggered_by         VARCHAR(255),              -- user_id | "scheduled" | "marketplace_submission"
    tenant_id            VARCHAR(255),
    status               VARCHAR(20) NOT NULL DEFAULT 'running',  -- running|passed|failed|partial|error
    total                INTEGER     NOT NULL DEFAULT 0,
    passed               INTEGER     NOT NULL DEFAULT 0,
    failed               INTEGER     NOT NULL DEFAULT 0,
    skipped              INTEGER     NOT NULL DEFAULT 0,
    app_package_id       UUID        NULL,          -- set for conformance runs
    ephemeral_tenant_id  VARCHAR(255) NULL,         -- disposable tenant the run provisioned
    started_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at         TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS hxtest_results (
    id            UUID         PRIMARY KEY,
    run_id        UUID         NOT NULL,
    test_id       VARCHAR(200) NOT NULL,
    test_name     VARCHAR(300),
    status        VARCHAR(20)  NOT NULL,            -- passed|failed|skipped|error
    duration_ms   INTEGER      NOT NULL DEFAULT 0,
    error_detail  TEXT         NULL,
    step_results  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    ran_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_hxtest_runs_suite   ON hxtest_runs (suite_id);
CREATE INDEX IF NOT EXISTS ix_hxtest_runs_package ON hxtest_runs (app_package_id);
CREATE INDEX IF NOT EXISTS ix_hxtest_results_run  ON hxtest_results (run_id);
CREATE INDEX IF NOT EXISTS ix_hxtest_suites_type  ON hxtest_suites (suite_type);
