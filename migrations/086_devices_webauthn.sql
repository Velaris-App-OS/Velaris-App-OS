-- 086: Group J — device-bound refresh tokens + WebAuthn passkeys.
-- auth_devices: one row per browser/machine a user logs in from; revoking a
-- device kills its whole refresh-token chain. webauthn_credentials: FIDO2
-- public keys (passkeys). webauthn_challenges: short-lived server-side
-- challenges (survive multi-process, swept on use/expiry).

ALTER TABLE refresh_tokens ADD COLUMN IF NOT EXISTS device_id UUID;
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_device ON refresh_tokens (device_id);

CREATE TABLE IF NOT EXISTS auth_devices (
    id               UUID         PRIMARY KEY,
    user_id          TEXT         NOT NULL,
    device_name      TEXT         NOT NULL DEFAULT 'Unknown device',
    user_agent_hash  VARCHAR(64)  NOT NULL,   -- sha256 of browser family + OS (not version — survives browser updates)
    first_ip         TEXT,
    last_ip          TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    revoked_at       TIMESTAMPTZ,
    revoked_by       TEXT
);
CREATE INDEX IF NOT EXISTS ix_auth_devices_user ON auth_devices (user_id);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id             UUID         PRIMARY KEY,
    user_id        TEXT         NOT NULL,
    credential_id  BYTEA        NOT NULL UNIQUE,
    public_key     BYTEA        NOT NULL,
    sign_count     BIGINT       NOT NULL DEFAULT 0,
    transports     JSONB        NOT NULL DEFAULT '[]',
    aaguid         TEXT,
    device_name    TEXT         NOT NULL DEFAULT 'Passkey',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_used_at   TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_webauthn_creds_user ON webauthn_credentials (user_id);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
    id          UUID         PRIMARY KEY,
    user_id     TEXT,                          -- NULL for discoverable (usernameless) login
    challenge   BYTEA        NOT NULL,
    purpose     TEXT         NOT NULL,         -- register | login | stepup
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ  NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_webauthn_chal_expires ON webauthn_challenges (expires_at);
