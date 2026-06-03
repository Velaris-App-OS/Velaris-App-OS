-- P48 HxConnect: Payment & Financial
-- Tracks payment requests initiated from case steps, with provider lifecycle state.

CREATE TABLE payment_requests (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,           -- step that triggered this payment
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    provider        VARCHAR(50) NOT NULL,           -- 'stripe' | 'paypal' | 'adyen'
    provider_ref    VARCHAR(255),                   -- Stripe payment_intent_id / session_id
    checkout_url    TEXT,                           -- hosted payment page sent to customer
    amount_cents    BIGINT      NOT NULL,
    currency        VARCHAR(10) NOT NULL DEFAULT 'usd',
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | processing | succeeded | failed | refunded | cancelled
    description     TEXT,
    metadata        JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX ix_payment_requests_case    ON payment_requests(case_id);
CREATE INDEX ix_payment_requests_ref     ON payment_requests(provider_ref);
CREATE INDEX ix_payment_requests_status  ON payment_requests(status);
CREATE INDEX ix_payment_requests_tenant  ON payment_requests(tenant_id);

-- Webhook event log: every inbound Stripe (or other provider) webhook received.
CREATE TABLE payment_webhook_events (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider      VARCHAR(50) NOT NULL,
    event_type    VARCHAR(255),                     -- e.g. 'payment_intent.succeeded'
    provider_ref  VARCHAR(255),                     -- payment_intent_id from the event
    payload       JSONB       NOT NULL DEFAULT '{}',
    verified      BOOLEAN     NOT NULL DEFAULT false,
    processed     BOOLEAN     NOT NULL DEFAULT false,
    error         TEXT,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_pwh_provider_ref ON payment_webhook_events(provider_ref);
CREATE INDEX ix_pwh_received     ON payment_webhook_events(received_at DESC);
