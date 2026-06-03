-- HELIX P36 — Audit chain, lineage, compliance reports
CREATE TABLE IF NOT EXISTS case_audit_log_chain (
    id            UUID PRIMARY KEY,
    sequence      INTEGER NOT NULL UNIQUE,
    audit_log_id  UUID NOT NULL,
    prev_hash     VARCHAR(64) NOT NULL,
    content_hash  VARCHAR(64) NOT NULL,
    sealed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_chain_seq      ON case_audit_log_chain(sequence);
CREATE INDEX IF NOT EXISTS idx_audit_chain_audit_id ON case_audit_log_chain(audit_log_id);
CREATE INDEX IF NOT EXISTS idx_audit_chain_sealed   ON case_audit_log_chain(sealed_at);

CREATE TABLE IF NOT EXISTS data_lineage_events (
    id            UUID PRIMARY KEY,
    case_id       UUID NOT NULL,
    kind          VARCHAR(64) NOT NULL,
    field_path    VARCHAR(512),
    before_value  JSONB,
    after_value   JSONB,
    actor_id      VARCHAR(255),
    source        VARCHAR(64) NOT NULL DEFAULT 'api',
    at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tenant_id     VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_lineage_case ON data_lineage_events(case_id);
CREATE INDEX IF NOT EXISTS idx_lineage_at   ON data_lineage_events(at);
CREATE INDEX IF NOT EXISTS idx_lineage_kind ON data_lineage_events(kind);

CREATE TABLE IF NOT EXISTS compliance_reports (
    id                UUID PRIMARY KEY,
    framework         VARCHAR(32) NOT NULL,
    period_start      TIMESTAMPTZ NOT NULL,
    period_end        TIMESTAMPTZ NOT NULL,
    generated_by      VARCHAR(255),
    generated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    summary           JSONB NOT NULL DEFAULT '{}'::jsonb,
    storage_key_json  VARCHAR(1024),
    storage_key_pdf   VARCHAR(1024),
    chain_verified    BOOLEAN NOT NULL DEFAULT FALSE,
    cadence           VARCHAR(16) NOT NULL DEFAULT 'on_demand',
    tenant_id         VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_compliance_framework    ON compliance_reports(framework);
CREATE INDEX IF NOT EXISTS idx_compliance_generated_at ON compliance_reports(generated_at);
