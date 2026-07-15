-- Portal v2 P5: CSAT ratings + AI-deflection feedback.
-- portal_csat: one rating per case, written by the linked customer after
-- resolution — feeds HxAnalytics/process mining.
-- portal_ask_feedback: was the pre-submit AI answer helpful? (deflection
-- tracking; anonymous, rate-limited at the endpoint).

CREATE TABLE IF NOT EXISTS portal_csat (
    case_id     UUID PRIMARY KEY REFERENCES case_instances(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES portal_customers(id) ON DELETE CASCADE,
    rating      SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS portal_ask_feedback (
    id          UUID PRIMARY KEY,
    tenant_slug VARCHAR(255) NOT NULL,
    question    TEXT NOT NULL,
    helpful     BOOLEAN NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_portal_ask_feedback_tenant
    ON portal_ask_feedback (tenant_slug, created_at);
