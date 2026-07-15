-- HxReplay P1 (Counterfactual Case Replay): replay runs + per-case results.
-- Replay NEVER writes to real case tables; these are its only output tables.

CREATE TABLE IF NOT EXISTS replay_runs (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(255),
    kind            VARCHAR(16)  NOT NULL DEFAULT 'single',   -- single | cohort
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',  -- pending | running | complete | failed
    branch_id       UUID,                                     -- HxBranch candidate config (NULL = ad-hoc override)
    candidate       JSONB        NOT NULL DEFAULT '{}'::jsonb, -- ad-hoc rule/threshold override (validated closed set)
    case_id         UUID,                                     -- single-case runs
    cohort_filter   JSONB        NOT NULL DEFAULT '{}'::jsonb, -- {case_type_id, from, to, max_cases}
    config_epoch    VARCHAR(32)  NOT NULL DEFAULT 'current+branch',
    summary         JSONB,                                    -- aggregate deltas + coverage + excluded-cohort profile
    result_digest   VARCHAR(64),                              -- sha256 of summary — anchored into the audit chain
    anchored        BOOLEAN      NOT NULL DEFAULT FALSE,
    error           TEXT,
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_replay_runs_tenant  ON replay_runs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_replay_runs_created ON replay_runs (created_at);
CREATE INDEX IF NOT EXISTS ix_replay_runs_status  ON replay_runs (status);

CREATE TABLE IF NOT EXISTS replay_results (
    id                     UUID PRIMARY KEY,
    run_id                 UUID         NOT NULL REFERENCES replay_runs(id) ON DELETE CASCADE,
    case_id                UUID         NOT NULL,
    tenant_id              VARCHAR(255),
    determinacy            VARCHAR(16)  NOT NULL DEFAULT 'determinate', -- determinate | indeterminate
    exclusion_reason       TEXT,
    divergence_point       VARCHAR(255),                                -- first activity the candidate config alters
    baseline_metrics       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    counterfactual_metrics JSONB,
    trace                  JSONB,                                       -- counterfactual tail, per-node classification
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_replay_results_run  ON replay_results (run_id);
CREATE INDEX IF NOT EXISTS ix_replay_results_case ON replay_results (case_id);
