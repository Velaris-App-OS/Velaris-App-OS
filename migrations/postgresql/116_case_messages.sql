-- Portal v2 P4: human case messaging (worker ↔ customer) + notification pref.
-- The thread is case-scoped; authors are principals ("user:{id}" or
-- "customer:{id}"). portal_visible lets workers post internal notes in the
-- same thread without leaking them to the portal.

CREATE TABLE IF NOT EXISTS case_messages (
    id             UUID PRIMARY KEY,
    case_id        UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    author         VARCHAR(255) NOT NULL,
    author_name    VARCHAR(255),
    body           TEXT NOT NULL,
    portal_visible BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_case_messages_case
    ON case_messages (case_id, created_at);

-- Customer notification preference (email on replies/updates).
ALTER TABLE portal_customers
    ADD COLUMN IF NOT EXISTS notify_email BOOLEAN NOT NULL DEFAULT TRUE;
