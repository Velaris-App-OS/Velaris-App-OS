-- HxEvolve cumulative-drift guardrail (§6 of the design, backlog item).
-- Each replay proof compares only against the immediately previous config, so a
-- chain of individually-improving merges can compound into a regression. The
-- baseline row pins the case-type's metrics when HxEvolve first saw it; after
-- every N merged HxEvolve changes the current metrics are compared against it.
-- A cumulative regression freezes further scans (frozen flag) and surfaces a
-- drift insight; an admin clears it via POST /hxevolve/config/{ct}/rebaseline.

CREATE TABLE IF NOT EXISTS hxevolve_baselines (
    case_type_id       UUID PRIMARY KEY,
    tenant_id          VARCHAR(255),
    metrics            JSONB        NOT NULL,     -- avg_duration_hours / conformance_rate / cases / window_days
    merged_at_baseline INTEGER      NOT NULL DEFAULT 0,   -- merged-change count when baseline was taken
    checked_through    INTEGER      NOT NULL DEFAULT 0,   -- merged-change count already drift-checked
    frozen             BOOLEAN      NOT NULL DEFAULT FALSE,
    frozen_reason      TEXT,
    created_by         VARCHAR(255),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    rebaselined_at     TIMESTAMPTZ
);

ALTER TABLE hxevolve_config
    ADD COLUMN IF NOT EXISTS drift_check_every_n_changes INTEGER NOT NULL DEFAULT 3;
