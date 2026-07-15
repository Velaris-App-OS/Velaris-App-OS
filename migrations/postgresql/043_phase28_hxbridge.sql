-- P28: HxBridge — Connector Protocol Foundation
-- connector_registry: all configured connectors with encrypted credentials
-- integration_calls:  log of every connector execution (feeds HxStream)
-- dead_letter_queue:  failed calls awaiting retry or manual resolution

BEGIN;

CREATE TABLE connector_registry (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255)  NOT NULL,
    connector_type  VARCHAR(100)  NOT NULL,   -- 'http' | 'webhook' | future: 'stripe' | 'salesforce'
    description     TEXT,
    config_schema   JSONB         NOT NULL DEFAULT '{}',  -- JSONSchema describing config fields
    config          JSONB         NOT NULL DEFAULT '{}',  -- connector configuration values
    credentials     JSONB         NOT NULL DEFAULT '{}',  -- encrypted at rest
    tenant_id       VARCHAR(255),
    enabled         BOOLEAN       NOT NULL DEFAULT TRUE,
    last_tested_at  TIMESTAMPTZ,
    last_test_ok    BOOLEAN,
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uq_connector_name_tenant
    ON connector_registry(name, COALESCE(tenant_id, ''));
CREATE INDEX ix_connector_type    ON connector_registry(connector_type);
CREATE INDEX ix_connector_tenant  ON connector_registry(tenant_id);
CREATE INDEX ix_connector_enabled ON connector_registry(enabled);


CREATE TABLE integration_calls (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id    UUID          REFERENCES connector_registry(id) ON DELETE SET NULL,
    case_id         UUID,
    step_id         VARCHAR(255),
    status          VARCHAR(20)   NOT NULL DEFAULT 'pending',
                                  -- pending | running | success | failed | retrying
    request         JSONB         NOT NULL DEFAULT '{}',
    response        JSONB,
    error           TEXT,
    latency_ms      INTEGER,
    retry_count     INTEGER       NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX ix_int_calls_connector ON integration_calls(connector_id);
CREATE INDEX ix_int_calls_case      ON integration_calls(case_id);
CREATE INDEX ix_int_calls_status    ON integration_calls(status);
CREATE INDEX ix_int_calls_created   ON integration_calls(created_at DESC);


CREATE TABLE dead_letter_queue (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id    UUID          REFERENCES connector_registry(id) ON DELETE SET NULL,
    case_id         UUID,
    step_id         VARCHAR(255),
    payload         JSONB         NOT NULL DEFAULT '{}',
    error           TEXT,
    retry_count     INTEGER       NOT NULL DEFAULT 0,
    max_retries     INTEGER       NOT NULL DEFAULT 3,
    next_retry_at   TIMESTAMPTZ,
    resolution      VARCHAR(20),  -- NULL | 'retried' | 'abandoned' | 'manual'
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX ix_dlq_connector   ON dead_letter_queue(connector_id);
CREATE INDEX ix_dlq_resolution  ON dead_letter_queue(resolution);
CREATE INDEX ix_dlq_retry       ON dead_letter_queue(next_retry_at)
    WHERE resolution IS NULL;

COMMIT;
