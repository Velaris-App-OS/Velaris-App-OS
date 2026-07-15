-- P51 HxConnect: Communications — Twilio SMS + Slack (outbound v1).

CREATE TABLE sms_messages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    provider        VARCHAR(50) NOT NULL DEFAULT 'twilio',
    to_number       VARCHAR(50) NOT NULL,
    from_number     VARCHAR(50),
    body            TEXT        NOT NULL,
    message_sid     VARCHAR(255),          -- Twilio MessageSid
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | queued | sent | delivered | failed | undelivered
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);

CREATE INDEX ix_sms_case   ON sms_messages(case_id);
CREATE INDEX ix_sms_status ON sms_messages(status);

CREATE TABLE slack_notifications (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    channel         VARCHAR(255),
    message         TEXT        NOT NULL,
    blocks          JSONB       NOT NULL DEFAULT '[]',
    slack_ts        VARCHAR(100),          -- Slack message timestamp (for threading)
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | sent | failed
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);

CREATE INDEX ix_slack_case   ON slack_notifications(case_id);
CREATE INDEX ix_slack_status ON slack_notifications(status);
