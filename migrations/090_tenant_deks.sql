-- 090: HxVault (#19) — per-tenant Data Encryption Keys (envelope encryption).
-- Each row holds a random 256-bit DEK, WRAPPED (AES-256-GCM) under the master
-- KEK that is rendered from OpenBao into the environment at startup.
-- tenant_id NULL = the platform DEK (covers tenantless/HxFusion case data).
-- Crypto-shredding (GDPR Art-17): set status='shredded' + delete wrapped_dek and
-- the field data encrypted under it becomes permanently unrecoverable.
-- DEK is RANDOM and STORED (never derived) — that is what makes shred real.

CREATE TABLE IF NOT EXISTS tenant_deks (
    id           UUID         PRIMARY KEY,
    tenant_id    UUID         NULL,                 -- NULL = platform DEK
    key_version  INTEGER      NOT NULL DEFAULT 1,   -- rotation-ready
    wrapped_dek  TEXT         NOT NULL,             -- base64(KEK-encrypted DEK)
    status       VARCHAR(20)  NOT NULL DEFAULT 'active',  -- active | shredded
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One active DEK per tenant (and one for the NULL/platform scope). A partial
-- unique index treats NULL as a single distinct key (plain UNIQUE would allow
-- many NULL rows in Postgres).
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_deks_tenant
    ON tenant_deks (tenant_id) WHERE tenant_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_deks_platform
    ON tenant_deks ((tenant_id IS NULL)) WHERE tenant_id IS NULL;
