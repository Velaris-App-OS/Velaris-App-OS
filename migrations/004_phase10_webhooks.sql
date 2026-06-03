-- HELIX Phase 10 Migration: Webhook subscriptions
BEGIN;

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    url             TEXT NOT NULL,
    secret          VARCHAR(255),
    events          TEXT[] DEFAULT '{}',
    case_type_id    UUID REFERENCES case_types(id),
    is_active       BOOLEAN DEFAULT true,
    headers         JSONB DEFAULT '{}',
    retry_count     INT DEFAULT 3,
    timeout_seconds INT DEFAULT 10,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id     UUID NOT NULL REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
    event_type          VARCHAR(100) NOT NULL,
    payload             JSONB NOT NULL,
    response_status     INT,
    response_body       TEXT,
    attempt             INT DEFAULT 1,
    delivered_at        TIMESTAMPTZ,
    next_retry_at       TIMESTAMPTZ,
    status              VARCHAR(20) DEFAULT 'pending',
    error_message       TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_sub
    ON webhook_deliveries(subscription_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status
    ON webhook_deliveries(status) WHERE status IN ('pending', 'failed');

COMMIT;
