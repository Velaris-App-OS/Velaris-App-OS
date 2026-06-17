-- 083: Transactional outbox for reliable webhook delivery
--
-- Replaces fire-and-forget HTTP dispatch in webhook_dispatcher.py.
-- Rows are written in the SAME transaction as the triggering business event,
-- then a background relay (OutboxRelay) claims and delivers them.
-- Crash-safe: if the relay dies mid-delivery the row is re-claimed after 5 minutes.

CREATE TABLE IF NOT EXISTS outbox (
    id              UUID         PRIMARY KEY,
    event_type      TEXT         NOT NULL,
    payload         JSONB        NOT NULL DEFAULT '{}',
    case_type_id    UUID         REFERENCES case_types(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    claimed_at      TIMESTAMPTZ,
    delivered_at    TIMESTAMPTZ,
    attempts        INTEGER      NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_outbox_pending
    ON outbox (created_at)
    WHERE delivered_at IS NULL;
