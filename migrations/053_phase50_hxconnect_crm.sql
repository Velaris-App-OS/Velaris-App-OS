-- P50 HxConnect: CRM (Salesforce) & Accounting (Xero) — outbound only v1.

CREATE TABLE crm_sync_records (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    provider        VARCHAR(50) NOT NULL DEFAULT 'salesforce',
    crm_object_type VARCHAR(100),          -- Contact, Case, Opportunity, Lead
    crm_record_id   VARCHAR(255),          -- Salesforce record ID
    crm_record_url  TEXT,                  -- link to record in CRM
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | synced | failed
    sync_data       JSONB       NOT NULL DEFAULT '{}',   -- fields sent
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at       TIMESTAMPTZ
);

CREATE INDEX ix_crm_case     ON crm_sync_records(case_id);
CREATE INDEX ix_crm_status   ON crm_sync_records(status);
CREATE INDEX ix_crm_record   ON crm_sync_records(crm_record_id);

CREATE TABLE invoice_records (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    provider        VARCHAR(50) NOT NULL DEFAULT 'xero',
    invoice_id      VARCHAR(255),          -- Xero InvoiceID
    invoice_number  VARCHAR(100),
    invoice_url     TEXT,
    amount_cents    BIGINT,
    currency        VARCHAR(10) NOT NULL DEFAULT 'usd',
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | draft | submitted | authorised | paid | voided
    contact_name    TEXT,
    line_items      JSONB       NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    issued_at       TIMESTAMPTZ
);

CREATE INDEX ix_inv_case    ON invoice_records(case_id);
CREATE INDEX ix_inv_status  ON invoice_records(status);
CREATE INDEX ix_inv_invoice ON invoice_records(invoice_id);
