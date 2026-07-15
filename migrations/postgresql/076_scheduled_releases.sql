-- Migration 076: Scheduled Releases (P66)
-- Tracks Velaris product feature releases with dates and release notes.

CREATE TABLE IF NOT EXISTS scheduled_releases (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_key   VARCHAR(100) NOT NULL UNIQUE,
    title         VARCHAR(255) NOT NULL,
    description   TEXT,
    release_notes TEXT,
    release_date  DATE,
    status        VARCHAR(20) NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'scheduled', 'released', 'rolled_back')),
    enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_scheduled_releases_status ON scheduled_releases(status);
CREATE INDEX IF NOT EXISTS ix_scheduled_releases_date   ON scheduled_releases(release_date);

-- Seed known hidden features
INSERT INTO scheduled_releases (feature_key, title, description, status, enabled)
VALUES
  ('marketplace',        'Marketplace',        'Browse and install third-party app packages', 'draft', FALSE),
  ('customer_accounts',  'Customer Accounts',  'Persistent portal customer identity with OTP login', 'draft', FALSE)
ON CONFLICT (feature_key) DO NOTHING;
