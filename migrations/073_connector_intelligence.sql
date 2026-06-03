-- Migration 073: Connector Intelligence
-- Adds outbound connector rules, field population audit, and indexes
-- for the connector_call step type and form-level connector lookups.

-- Outbound connector trigger rules (case event → fire connector)
CREATE TABLE IF NOT EXISTS outbound_connector_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(255) NOT NULL,
    name            TEXT NOT NULL,
    trigger_event   VARCHAR(50)  NOT NULL,
    case_type_id    UUID,
    condition_expr  JSONB,
    connector_id    UUID REFERENCES connector_registry(id) ON DELETE SET NULL,
    input_mapping   JSONB NOT NULL DEFAULT '{}',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ocr_tenant    ON outbound_connector_rules (tenant_id);
CREATE INDEX IF NOT EXISTS ix_ocr_connector ON outbound_connector_rules (connector_id);
CREATE INDEX IF NOT EXISTS ix_ocr_enabled   ON outbound_connector_rules (enabled);

-- Field population audit: every connector-sourced form value is logged here
CREATE TABLE IF NOT EXISTS field_population_audit (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(255) NOT NULL,
    case_id         UUID,
    form_id         VARCHAR(255),
    field_key       VARCHAR(255) NOT NULL,
    connector_id    UUID REFERENCES connector_registry(id) ON DELETE SET NULL,
    user_id         VARCHAR(255),
    response_hash   VARCHAR(64),
    populated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_fpa_case      ON field_population_audit (case_id);
CREATE INDEX IF NOT EXISTS ix_fpa_connector ON field_population_audit (connector_id);
CREATE INDEX IF NOT EXISTS ix_fpa_user      ON field_population_audit (user_id);
