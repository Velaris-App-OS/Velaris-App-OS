-- P48b: Payment disbursements — pay-to-customer flow on case steps.

CREATE TABLE payment_disbursements (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    amount_cents    BIGINT      NOT NULL,
    currency        VARCHAR(10) NOT NULL DEFAULT 'usd',
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | confirmed | processing | completed | failed | cancelled
    description     TEXT,
    bank_reference  TEXT,
    notes           TEXT,
    confirmed_by    TEXT,
    confirmed_at    TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_pd_case    ON payment_disbursements(case_id);
CREATE INDEX ix_pd_status  ON payment_disbursements(status);
CREATE INDEX ix_pd_tenant  ON payment_disbursements(tenant_id);
