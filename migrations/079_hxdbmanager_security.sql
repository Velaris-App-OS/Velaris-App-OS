-- Migration 079: HxDBManager Security — revoked sessions + DML before-image log

-- Instant JWT revocation on breach or manual disable.
-- Auth middleware checks this table (30s in-memory cache) on every request.
CREATE TABLE IF NOT EXISTS revoked_sessions (
    token_hash  VARCHAR(64) PRIMARY KEY,  -- sha256 of raw JWT string
    user_id     VARCHAR(255) NOT NULL,
    revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason      TEXT,
    revoked_by  VARCHAR(255),             -- "system:breach_detection" or admin user_id
    expires_at  TIMESTAMPTZ NOT NULL      -- copy of JWT exp; used for cleanup cron
);

CREATE INDEX IF NOT EXISTS ix_revoked_sessions_user    ON revoked_sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_revoked_sessions_expires ON revoked_sessions(expires_at);

-- Before-image snapshots for DML rollback via HxDBManager.
-- old_rows = rows as they existed before the operation (DELETE/UPDATE).
-- new_rows = rows after the operation (UPDATE/INSERT), when capturable.
CREATE TABLE IF NOT EXISTS dml_before_image (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      VARCHAR(255) NOT NULL,
    user_id        VARCHAR(255) NOT NULL,
    operation      VARCHAR(10)  NOT NULL,   -- DELETE | UPDATE | INSERT
    table_hint     VARCHAR(255),            -- extracted table name when parseable
    original_sql   TEXT NOT NULL,
    old_rows       JSONB,                   -- before-image rows
    new_rows       JSONB,                   -- after-image rows (UPDATE / INSERT)
    row_count      INTEGER,
    capture_method VARCHAR(30)  NOT NULL,   -- returning | pre_select | partial
    captured_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_dml_before_tenant   ON dml_before_image(tenant_id);
CREATE INDEX IF NOT EXISTS ix_dml_before_user     ON dml_before_image(user_id);
CREATE INDEX IF NOT EXISTS ix_dml_before_captured ON dml_before_image(captured_at DESC);
