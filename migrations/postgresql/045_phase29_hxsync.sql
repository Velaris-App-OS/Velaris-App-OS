-- P29: HxSync — Data Pipeline & Warehouse Bridge
-- sync_destinations:    target DWH/stream endpoint config per tenant
-- sync_runs:            per-execution record (CDC watermark, rows, errors)
-- sync_field_mappings:  column rename + transform rules per case type
-- sync_redaction_rules: PII field handling (hash/drop/mask) per destination

CREATE TABLE sync_destinations (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    dest_type       VARCHAR(30)  NOT NULL DEFAULT 'duckdb',
    connection_config JSONB      NOT NULL DEFAULT '{}',
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    tenant_id       VARCHAR(255),
    created_by      VARCHAR(255),
    last_synced_at  TIMESTAMPTZ,
    last_sync_status VARCHAR(20) DEFAULT 'never',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_sync_dest_tenant  ON sync_destinations (tenant_id);
CREATE INDEX ix_sync_dest_enabled ON sync_destinations (enabled);

CREATE TABLE sync_runs (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    destination_id  UUID         NOT NULL REFERENCES sync_destinations(id) ON DELETE CASCADE,
    status          VARCHAR(20)  NOT NULL DEFAULT 'running',
    rows_synced     INTEGER      NOT NULL DEFAULT 0,
    error_msg       TEXT,
    watermark_from  TIMESTAMPTZ,
    watermark_to    TIMESTAMPTZ,
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_sync_runs_dest    ON sync_runs (destination_id);
CREATE INDEX ix_sync_runs_status  ON sync_runs (status);
CREATE INDEX ix_sync_runs_started ON sync_runs (started_at DESC);

CREATE TABLE sync_field_mappings (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    destination_id  UUID         NOT NULL REFERENCES sync_destinations(id) ON DELETE CASCADE,
    case_type_id    VARCHAR(255),
    source_field    VARCHAR(255) NOT NULL,
    dest_column     VARCHAR(255) NOT NULL,
    transform       VARCHAR(30)  NOT NULL DEFAULT 'passthrough',
    pii             BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_sync_fmap_dest ON sync_field_mappings (destination_id);

CREATE TABLE sync_redaction_rules (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    destination_id  UUID         NOT NULL REFERENCES sync_destinations(id) ON DELETE CASCADE,
    case_type_id    VARCHAR(255),
    field_path      VARCHAR(255) NOT NULL,
    action          VARCHAR(10)  NOT NULL DEFAULT 'hash',
    reason          TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_sync_redact_dest ON sync_redaction_rules (destination_id);
