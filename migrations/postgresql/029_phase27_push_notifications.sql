-- Migration 029 — P27 Push Notifications
-- Tables: push_device_tokens, notification_preferences,
--         case_type_notification_overrides, notification_logs

CREATE TABLE IF NOT EXISTS push_device_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         VARCHAR(255) NOT NULL,
    channel         VARCHAR(32)  NOT NULL,   -- fcm | apns | webpush
    token           TEXT         NOT NULL,
    platform        VARCHAR(64),             -- android | ios | web
    label           VARCHAR(255),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    last_seen_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    tenant_id       VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_push_device_user    ON push_device_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_push_device_channel ON push_device_tokens (channel);
CREATE INDEX IF NOT EXISTS idx_push_device_active  ON push_device_tokens (is_active);

CREATE TABLE IF NOT EXISTS notification_preferences (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     VARCHAR(255) NOT NULL,
    event_type  VARCHAR(128) NOT NULL,
    channels    JSONB        NOT NULL DEFAULT '[]',
    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_notif_pref_user_event ON notification_preferences (user_id, event_type);

CREATE TABLE IF NOT EXISTS case_type_notification_overrides (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_type_id UUID         NOT NULL REFERENCES case_types(id) ON DELETE CASCADE,
    event_type   VARCHAR(128) NOT NULL,
    channels     JSONB        NOT NULL DEFAULT '[]',
    enabled      BOOLEAN      NOT NULL DEFAULT TRUE,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (case_type_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_ctno_case_type_event ON case_type_notification_overrides (case_type_id, event_type);

CREATE TABLE IF NOT EXISTS notification_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id   UUID,
    user_id     VARCHAR(255) NOT NULL,
    event_type  VARCHAR(128) NOT NULL,
    channel     VARCHAR(32)  NOT NULL,
    status      VARCHAR(32)  NOT NULL,   -- delivered | failed
    error       TEXT,
    sent_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notif_log_device ON notification_logs (device_id);
CREATE INDEX IF NOT EXISTS idx_notif_log_user   ON notification_logs (user_id);
CREATE INDEX IF NOT EXISTS idx_notif_log_status ON notification_logs (status);
CREATE INDEX IF NOT EXISTS idx_notif_log_sent   ON notification_logs (sent_at);
