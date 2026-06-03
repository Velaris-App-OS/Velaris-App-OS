-- P53 HxConnect: Developer & Custom Connectors

CREATE TABLE webhook_receiver_rules (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL,
    -- how to find the case from the incoming payload
    case_id_field       TEXT,       -- JSONPath in payload that contains the case UUID directly
    match_case_field    TEXT,       -- case data field name to match on (e.g. "reference_number")
    match_payload_field TEXT,       -- JSONPath in payload whose value to match against
    -- what to do once case is found
    field_updates   JSONB       NOT NULL DEFAULT '{}',   -- {"case_field": "payload.jsonpath"}
    advance_stage   BOOLEAN     NOT NULL DEFAULT false,
    enabled         BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_wrr_connector ON webhook_receiver_rules(connector_id);
CREATE INDEX ix_wrr_tenant    ON webhook_receiver_rules(tenant_id);

CREATE TABLE webhook_receiver_events (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    rule_id         UUID        REFERENCES webhook_receiver_rules(id) ON DELETE SET NULL,
    payload         JSONB       NOT NULL DEFAULT '{}',
    matched_case_id UUID,
    status          VARCHAR(50) NOT NULL DEFAULT 'received',
    -- received | matched | no_match | error
    error           TEXT,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX ix_wre_connector ON webhook_receiver_events(connector_id);
CREATE INDEX ix_wre_status    ON webhook_receiver_events(status);
CREATE INDEX ix_wre_case      ON webhook_receiver_events(matched_case_id);
