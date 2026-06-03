-- 068: Auth hardening — settings table + enforce first-login password change

CREATE TABLE IF NOT EXISTS helix_settings (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT
);

INSERT INTO helix_settings (key, value) VALUES ('token_expiry_days', '60')
ON CONFLICT (key) DO NOTHING;

-- Every user must change their default password on first login
UPDATE helix_users SET password_change_required = TRUE;
