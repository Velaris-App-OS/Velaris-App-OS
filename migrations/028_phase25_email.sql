-- HELIX P25 — Email Integration
CREATE TABLE IF NOT EXISTS email_accounts (
    id                     UUID PRIMARY KEY,
    name                   VARCHAR(255) NOT NULL,
    address                VARCHAR(320) NOT NULL,
    smtp_host              VARCHAR(255) NOT NULL,
    smtp_port              INTEGER NOT NULL DEFAULT 587,
    smtp_username          VARCHAR(255),
    smtp_password          VARCHAR(1024),
    smtp_use_tls           BOOLEAN NOT NULL DEFAULT TRUE,
    imap_host              VARCHAR(255),
    imap_port              INTEGER NOT NULL DEFAULT 993,
    imap_username          VARCHAR(255),
    imap_password          VARCHAR(1024),
    imap_use_ssl           BOOLEAN NOT NULL DEFAULT TRUE,
    imap_folder            VARCHAR(255) NOT NULL DEFAULT 'INBOX',
    poll_interval_seconds  INTEGER NOT NULL DEFAULT 15,
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    is_default_outbound    BOOLEAN NOT NULL DEFAULT FALSE,
    tenant_id              VARCHAR(64),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_email_accounts_active ON email_accounts(is_active);
CREATE INDEX IF NOT EXISTS idx_email_accounts_tenant ON email_accounts(tenant_id);

CREATE TABLE IF NOT EXISTS email_templates (
    id            UUID PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    subject       VARCHAR(998) NOT NULL,
    body_text     TEXT NOT NULL,
    body_html     TEXT,
    engine        VARCHAR(16) NOT NULL DEFAULT 'jinja2',
    scope         VARCHAR(32) NOT NULL DEFAULT 'global',
    case_type_id  UUID REFERENCES case_types(id),
    tenant_id     VARCHAR(64),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_email_templates_scope     ON email_templates(scope);
CREATE INDEX IF NOT EXISTS idx_email_templates_case_type ON email_templates(case_type_id);

CREATE TABLE IF NOT EXISTS email_messages (
    id              UUID PRIMARY KEY,
    case_id         UUID,
    direction       VARCHAR(16) NOT NULL,
    account_id      UUID,
    message_id      VARCHAR(998),
    in_reply_to     VARCHAR(998),
    "references"    JSONB NOT NULL DEFAULT '[]'::jsonb,
    from_address    VARCHAR(320) NOT NULL DEFAULT '',
    to_addresses    JSONB NOT NULL DEFAULT '[]'::jsonb,
    cc_addresses    JSONB NOT NULL DEFAULT '[]'::jsonb,
    subject         TEXT NOT NULL DEFAULT '',
    body_text       TEXT NOT NULL DEFAULT '',
    body_html       TEXT,
    raw_headers     JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          VARCHAR(32) NOT NULL DEFAULT 'received',
    error_message   TEXT,
    is_read         BOOLEAN NOT NULL DEFAULT FALSE,
    sent_at         TIMESTAMPTZ,
    received_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tenant_id       VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_email_messages_case      ON email_messages(case_id);
CREATE INDEX IF NOT EXISTS idx_email_messages_msgid     ON email_messages(message_id);
CREATE INDEX IF NOT EXISTS idx_email_messages_direction ON email_messages(direction);
CREATE INDEX IF NOT EXISTS idx_email_messages_status    ON email_messages(status);
CREATE INDEX IF NOT EXISTS idx_email_messages_received  ON email_messages(received_at);
CREATE INDEX IF NOT EXISTS idx_email_messages_unread    ON email_messages(is_read);
