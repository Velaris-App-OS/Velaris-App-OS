-- HxNexus Operator (MCP) P2: durable idempotency store for mutating tool calls.
-- Every MCP write carries an idempotency_key; a retried/duplicated AI call with
-- the same (user, key) replays the recorded result instead of re-applying.
-- Keys are scoped per user, so one caller's key namespace can never collide with
-- or probe another's.

CREATE TABLE IF NOT EXISTS mcp_idempotency_keys (
    id              UUID PRIMARY KEY,
    user_id         VARCHAR(255) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    tool_name       VARCHAR(100) NOT NULL,
    request_hash    VARCHAR(64)  NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'pending',   -- pending | done
    response_json   JSONB,
    is_error        BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_mcp_idem_user_key UNIQUE (user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_mcp_idem_created ON mcp_idempotency_keys (created_at);
