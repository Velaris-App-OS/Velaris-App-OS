-- Migration 078: HxDBManager — AI-Powered Database Operations
-- Query audit log for all SQL run through the DB Manager.

CREATE TABLE IF NOT EXISTS db_manager_query_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     VARCHAR(255) NOT NULL,
    user_id       VARCHAR(255) NOT NULL,
    query_text    TEXT NOT NULL,
    query_hash    VARCHAR(64) NOT NULL,   -- sha256 for deduplication
    duration_ms   INTEGER,
    rows_affected INTEGER,
    status        VARCHAR(20) NOT NULL DEFAULT 'success'
                  CHECK (status IN ('success', 'error', 'timeout', 'rejected')),
    error_detail  TEXT,
    ran_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_dbmgr_log_tenant   ON db_manager_query_log(tenant_id);
CREATE INDEX IF NOT EXISTS ix_dbmgr_log_user      ON db_manager_query_log(user_id);
CREATE INDEX IF NOT EXISTS ix_dbmgr_log_ran_at    ON db_manager_query_log(ran_at DESC);
CREATE INDEX IF NOT EXISTS ix_dbmgr_log_hash      ON db_manager_query_log(query_hash);
