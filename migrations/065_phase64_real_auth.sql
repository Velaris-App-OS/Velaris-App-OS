-- ============================================================
-- P64 — ENH-10: Real Authentication
-- Adds helix_users table for bcrypt password auth, account
-- lockout, TOTP MFA, and SSO provider registry.
-- ============================================================

CREATE TABLE IF NOT EXISTS helix_users (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    username                 TEXT        NOT NULL UNIQUE,
    email                    TEXT        NOT NULL UNIQUE,
    display_name             TEXT,
    password_hash            TEXT,       -- bcrypt hash; NULL for SSO-only users
    roles                    JSONB       NOT NULL DEFAULT '["viewer"]',
    is_active                BOOLEAN     NOT NULL DEFAULT TRUE,
    failed_attempts          INTEGER     NOT NULL DEFAULT 0,
    locked_until             TIMESTAMPTZ,
    password_change_required BOOLEAN     NOT NULL DEFAULT FALSE,
    mfa_enabled              BOOLEAN     NOT NULL DEFAULT FALSE,
    mfa_secret_enc           JSONB,      -- AES-256 encrypted TOTP secret
    sso_provider             TEXT,       -- 'google'|'github'|'azure'|'saml'
    sso_subject              TEXT,       -- provider's unique user ID
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_helix_users_email    ON helix_users(email);
CREATE INDEX IF NOT EXISTS ix_helix_users_username ON helix_users(username);
CREATE INDEX IF NOT EXISTS ix_helix_users_sso      ON helix_users(sso_provider, sso_subject);

-- Password reset OTPs (short-lived, single-use)
CREATE TABLE IF NOT EXISTS auth_otp (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL REFERENCES helix_users(id) ON DELETE CASCADE,
    otp_hash   TEXT        NOT NULL,    -- bcrypt hash of the 6-digit OTP
    purpose    TEXT        NOT NULL,    -- 'password_reset' | 'mfa_enrol'
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_auth_otp_user ON auth_otp(user_id);

-- SSO provider configuration per tenant
CREATE TABLE IF NOT EXISTS sso_providers (
    id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         TEXT,   -- NULL = global (all tenants)
    provider          TEXT    NOT NULL,   -- 'google'|'github'|'azure'|'saml'
    client_id         TEXT    NOT NULL,
    client_secret_enc JSONB,  -- AES-256 encrypted
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    config            JSONB   NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_sso_tenant ON sso_providers(tenant_id, provider);

-- Seed default admin user (password: "helix-admin" — must change on first login)
INSERT INTO helix_users (username, email, display_name, password_hash, roles, password_change_required)
VALUES (
    'admin',
    'admin@helix.local',
    'Helix Administrator',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TiGG9.WZk4dMn6JxVQSBVHmQdm/6',  -- "helix-admin"
    '["admin","designer","case_worker","devops","integration","security"]',
    TRUE
) ON CONFLICT (username) DO NOTHING;
