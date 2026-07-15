-- Migration 075: Customer Accounts (P65)
-- Persistent portal customer identity with OTP login and preferred email support.

CREATE TABLE IF NOT EXISTS portal_customers (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    primary_email    VARCHAR(255) NOT NULL,
    alt_email        VARCHAR(255),
    preferred_email  VARCHAR(10) NOT NULL DEFAULT 'primary' CHECK (preferred_email IN ('primary', 'alt')),
    display_name     VARCHAR(255) NOT NULL,
    phone            VARCHAR(64),
    verified         BOOLEAN NOT NULL DEFAULT FALSE,
    otp_code         VARCHAR(64),   -- SHA-256 hash of the 6-digit code
    otp_expires_at   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_portal_customer_email_tenant UNIQUE (tenant_id, primary_email)
);

CREATE INDEX IF NOT EXISTS ix_portal_customers_tenant ON portal_customers(tenant_id);
CREATE INDEX IF NOT EXISTS ix_portal_customers_email  ON portal_customers(primary_email);

CREATE TABLE IF NOT EXISTS portal_customer_cases (
    customer_id  UUID NOT NULL REFERENCES portal_customers(id) ON DELETE CASCADE,
    case_id      UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    linked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (customer_id, case_id)
);

CREATE INDEX IF NOT EXISTS ix_pcc_customer ON portal_customer_cases(customer_id);
CREATE INDEX IF NOT EXISTS ix_pcc_case     ON portal_customer_cases(case_id);
