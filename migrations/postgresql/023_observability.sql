-- HELIX P23 — Observability tables
CREATE TABLE IF NOT EXISTS telemetry_events (
    id          UUID PRIMARY KEY,
    event_type  VARCHAR(128) NOT NULL,
    severity    VARCHAR(16)  NOT NULL DEFAULT 'INFO',
    payload     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    request_id  VARCHAR(64),
    trace_id    VARCHAR(64),
    tenant_id   VARCHAR(64),
    user_id     VARCHAR(128),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_telemetry_events_type     ON telemetry_events(event_type);
CREATE INDEX IF NOT EXISTS ix_telemetry_events_severity ON telemetry_events(severity);
CREATE INDEX IF NOT EXISTS ix_telemetry_events_tenant   ON telemetry_events(tenant_id);
CREATE INDEX IF NOT EXISTS ix_telemetry_events_trace    ON telemetry_events(trace_id);
CREATE INDEX IF NOT EXISTS ix_telemetry_events_created  ON telemetry_events(created_at DESC);

CREATE TABLE IF NOT EXISTS health_check_results (
    id          UUID PRIMARY KEY,
    component   VARCHAR(64)  NOT NULL,
    status      VARCHAR(16)  NOT NULL,
    latency_ms  DOUBLE PRECISION,
    detail      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    checked_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hcr_component ON health_check_results(component);
CREATE INDEX IF NOT EXISTS ix_hcr_status    ON health_check_results(status);
CREATE INDEX IF NOT EXISTS ix_hcr_checked   ON health_check_results(checked_at DESC);
