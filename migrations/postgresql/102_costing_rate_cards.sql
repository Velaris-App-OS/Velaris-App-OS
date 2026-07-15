-- HxReplay P4 / Case Costing: per-tenant rate cards (cost = manual time × rate).
-- Commercially sensitive — API access is HxGuard-gated (costing.rates).

CREATE TABLE IF NOT EXISTS rate_cards (
    id           UUID PRIMARY KEY,
    tenant_id    VARCHAR(255),
    role         VARCHAR(100) NOT NULL DEFAULT '*',   -- '*' = tenant default (P4 uses this)
    hourly_rate  DOUBLE PRECISION NOT NULL,
    currency     VARCHAR(8)   NOT NULL DEFAULT 'USD',
    created_by   VARCHAR(255),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_cards_tenant_role ON rate_cards (tenant_id, role);
