-- HxEvolve P3: per-case-type objective/guardrail configuration + scan scheduling.
-- Defaults are conservative (§4): tight guardrails, daily frequency, scheduled
-- scanning OFF until an admin opts the case type in.

CREATE TABLE IF NOT EXISTS hxevolve_config (
    case_type_id         UUID PRIMARY KEY,
    tenant_id            VARCHAR(255),
    min_improvement      DOUBLE PRECISION NOT NULL DEFAULT 0.10,
    max_auto_ratio_rise  DOUBLE PRECISION NOT NULL DEFAULT 0.15,
    min_coverage         DOUBLE PRECISION NOT NULL DEFAULT 0.7,
    min_determinate      INTEGER          NOT NULL DEFAULT 50,
    scan_frequency_hours INTEGER          NOT NULL DEFAULT 24,
    scan_enabled         BOOLEAN          NOT NULL DEFAULT FALSE,
    updated_by           VARCHAR(255),
    updated_at           TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hxevolve_cfg_tenant  ON hxevolve_config (tenant_id);
CREATE INDEX IF NOT EXISTS ix_hxevolve_cfg_enabled ON hxevolve_config (scan_enabled);
