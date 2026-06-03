-- Migration 074: HxDeploy Delivery Methods
-- Adds delivery_method, webhook_url, webhook_secret, import_api_key
-- to environment_registry so each environment can use:
--   manual  → governance-only (current behaviour)
--   webhook → POST HMAC-signed payload to webhook_url; CI/CD calls back
--   push    → serialise bundle and POST to env.url/api/v1/deploy/import

ALTER TABLE environment_registry
    ADD COLUMN IF NOT EXISTS delivery_method  VARCHAR(20)  NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS webhook_url      TEXT,
    ADD COLUMN IF NOT EXISTS webhook_secret   TEXT,
    ADD COLUMN IF NOT EXISTS import_api_key   TEXT;

-- Constrain delivery_method to known values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_env_delivery_method'
    ) THEN
        ALTER TABLE environment_registry
            ADD CONSTRAINT chk_env_delivery_method
            CHECK (delivery_method IN ('manual', 'webhook', 'push'));
    END IF;
END $$;

-- Index for fast lookup of push/webhook environments
CREATE INDEX IF NOT EXISTS ix_env_delivery ON environment_registry (delivery_method);
