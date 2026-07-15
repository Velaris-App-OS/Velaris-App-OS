-- SD-6/SD-7: Connector credential expiry and rotation tracking
ALTER TABLE connector_registry
    ADD COLUMN IF NOT EXISTS credential_expires_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS credentials_updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS ix_connector_cred_expires
    ON connector_registry (credential_expires_at)
    WHERE credential_expires_at IS NOT NULL;
