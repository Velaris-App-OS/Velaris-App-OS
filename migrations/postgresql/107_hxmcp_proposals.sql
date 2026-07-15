-- HxNexus Operator (MCP) P3: human-in-the-loop for stateful actions.
-- A stateful tool call (advance stage / set status / close / create) does not
-- execute when confirmation is required: it records a proposal that a human
-- reviews and confirms. The proposal row is the idempotency anchor — its status
-- gates execution to exactly once.

CREATE TABLE IF NOT EXISTS mcp_action_proposals (
    id             UUID PRIMARY KEY,
    user_id        VARCHAR(255) NOT NULL,      -- the (AI-session) proposer
    tool_name      VARCHAR(100) NOT NULL,
    arguments_json JSONB        NOT NULL,
    case_id        UUID,                        -- target case, when applicable
    summary        TEXT,
    status         VARCHAR(20)  NOT NULL DEFAULT 'pending',  -- pending|executed|rejected|expired
    result_json    JSONB,
    is_error       BOOLEAN      NOT NULL DEFAULT FALSE,
    decided_by     VARCHAR(255),
    decided_at     TIMESTAMPTZ,
    expires_at     TIMESTAMPTZ  NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mcp_prop_status  ON mcp_action_proposals (status);
CREATE INDEX IF NOT EXISTS ix_mcp_prop_user    ON mcp_action_proposals (user_id);
CREATE INDEX IF NOT EXISTS ix_mcp_prop_case    ON mcp_action_proposals (case_id);
