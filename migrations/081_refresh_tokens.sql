-- 081: Refresh token table for A3 short-lived JWT + refresh flow

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash  TEXT        PRIMARY KEY,
    user_id     TEXT        NOT NULL,
    jti         TEXT        NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    revoked_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_refresh_tokens_user    ON refresh_tokens (user_id);
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_expires ON refresh_tokens (expires_at);
