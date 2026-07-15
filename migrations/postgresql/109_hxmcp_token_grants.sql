-- HxNexus Operator (MCP) P4: external-agent scoped-token grants.
-- The scoped JWT (token_use="mcp") is stateless; this table is its server-side
-- anchor: listing, audit, and INSTANT revocation — the MCP transport checks the
-- grant row on every call, so revoking here cuts an agent off immediately even
-- while its JWT is still time-valid. A grant only ever restricts the grantor's
-- own authority (authorization runs as the grantor; the grant shrinks tools).

CREATE TABLE IF NOT EXISTS mcp_token_grants (
    id          UUID PRIMARY KEY,                  -- = JWT jti
    user_id     VARCHAR(255) NOT NULL,             -- grantor
    tenant_id   VARCHAR(255),
    tools       JSONB        NOT NULL,             -- granted tool names
    label       VARCHAR(255),                      -- e.g. which agent this is for
    revoked     BOOLEAN      NOT NULL DEFAULT FALSE,
    revoked_at  TIMESTAMPTZ,
    revoked_by  VARCHAR(255),
    expires_at  TIMESTAMPTZ  NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mcp_grant_user    ON mcp_token_grants (user_id);
CREATE INDEX IF NOT EXISTS ix_mcp_grant_expires ON mcp_token_grants (expires_at);
