-- HxMeet P2: participants of a case session (embedded/LiveKit driver).
-- A row is created at invite time (guest) or first token mint (worker);
-- joined_at/left_at are stamped by LiveKit webhooks. Guest invites are
-- single-use: only the SHA-256 of the invite token is stored (same posture
-- as the portal-customer OTP), consumed atomically on exchange.
-- consent_recorded_at stays NULL until the P3 sealed-recording path.

CREATE TABLE IF NOT EXISTS case_session_participants (
    id                  UUID PRIMARY KEY,
    session_id          UUID         NOT NULL REFERENCES case_sessions(id) ON DELETE CASCADE,
    tenant_id           VARCHAR(255),
    identity            VARCHAR(512) NOT NULL,           -- user:{id} | customer:{uuid} | email:{addr}
    display_name        VARCHAR(255),
    role                VARCHAR(20)  NOT NULL DEFAULT 'guest',  -- host | guest
    invited_by          VARCHAR(255),                    -- worker user_id (NULL for the host's own row)
    invite_token_hash   VARCHAR(64) UNIQUE,              -- SHA-256, NULL for internal joins
    invite_expires_at   TIMESTAMPTZ,
    token_used_at       TIMESTAMPTZ,
    joined_at           TIMESTAMPTZ,
    left_at             TIMESTAMPTZ,
    consent_recorded_at TIMESTAMPTZ,                     -- P3
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_csp_session ON case_session_participants (session_id);
CREATE INDEX IF NOT EXISTS ix_csp_tenant  ON case_session_participants (tenant_id);
