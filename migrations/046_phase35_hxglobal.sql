-- P35: HxGlobal — Multi-Region & Data Sovereignty
-- region_registry:          known regions with connection config
-- sovereignty_rules:        per-tenant / per-case-type data residency rules
-- tenant_region_assignments: which region is authoritative for a tenant
-- region_health_log:        point-in-time health snapshots per region

CREATE TABLE region_registry (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(100) NOT NULL UNIQUE,
    provider        VARCHAR(20)  NOT NULL DEFAULT 'local',
    location        VARCHAR(100),
    endpoint        VARCHAR(500),
    connection_config JSONB      NOT NULL DEFAULT '{}',
    is_primary      BOOLEAN      NOT NULL DEFAULT FALSE,
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_region_enabled ON region_registry (enabled);

CREATE TABLE sovereignty_rules (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(255),
    case_type_id    VARCHAR(255),
    region_id       UUID         NOT NULL REFERENCES region_registry(id) ON DELETE CASCADE,
    regulation      VARCHAR(50)  NOT NULL DEFAULT 'GDPR',
    description     TEXT,
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_sov_tenant     ON sovereignty_rules (tenant_id);
CREATE INDEX ix_sov_case_type  ON sovereignty_rules (case_type_id);
CREATE INDEX ix_sov_region     ON sovereignty_rules (region_id);

CREATE TABLE tenant_region_assignments (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(255) NOT NULL,
    region_id       UUID         NOT NULL REFERENCES region_registry(id) ON DELETE CASCADE,
    assignment_type VARCHAR(20)  NOT NULL DEFAULT 'primary',
    migrated_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, region_id, assignment_type)
);

CREATE INDEX ix_tra_tenant ON tenant_region_assignments (tenant_id);

CREATE TABLE region_health_log (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    region_id       UUID         NOT NULL REFERENCES region_registry(id) ON DELETE CASCADE,
    status          VARCHAR(20)  NOT NULL DEFAULT 'healthy',
    latency_ms      INTEGER,
    active_cases    INTEGER,
    replication_lag_ms INTEGER,
    error_msg       TEXT,
    recorded_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_rhl_region     ON region_health_log (region_id);
CREATE INDEX ix_rhl_recorded   ON region_health_log (recorded_at DESC);

CREATE TABLE region_access_log (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    region_id       UUID         NOT NULL REFERENCES region_registry(id) ON DELETE CASCADE,
    tenant_id       VARCHAR(255),
    actor_id        VARCHAR(255),
    action          VARCHAR(50)  NOT NULL,
    resource        VARCHAR(255),
    legal_basis     VARCHAR(100),
    recorded_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_ral_region   ON region_access_log (region_id);
CREATE INDEX ix_ral_tenant   ON region_access_log (tenant_id);
CREATE INDEX ix_ral_recorded ON region_access_log (recorded_at DESC);
