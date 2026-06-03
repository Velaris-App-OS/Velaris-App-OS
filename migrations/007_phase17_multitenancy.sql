-- HELIX Phase 17 Migration: Multi-tenancy
BEGIN;

-- Tenants table
CREATE TABLE IF NOT EXISTS tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            VARCHAR(100) UNIQUE NOT NULL,
    name            VARCHAR(255) NOT NULL,
    description     TEXT DEFAULT '',
    status          VARCHAR(30) DEFAULT 'active',  -- active, suspended, archived
    settings        JSONB DEFAULT '{}',
    max_cases       INT,                            -- resource limits
    max_users       INT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Create default tenant for migrating existing data
INSERT INTO tenants (slug, name, description)
VALUES ('default', 'Default Tenant', 'Default tenant for pre-Phase 17 data')
ON CONFLICT (slug) DO NOTHING;

-- Tenant memberships (user → tenant mapping)
CREATE TABLE IF NOT EXISTS tenant_memberships (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         VARCHAR(255) NOT NULL,
    role            VARCHAR(50) DEFAULT 'member',   -- owner, admin, member, viewer
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(tenant_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_memberships_user
    ON tenant_memberships(user_id);

-- Add tenant_id to all tenant-scoped tables
DO $$
DECLARE
    default_tenant_id UUID;
BEGIN
    SELECT id INTO default_tenant_id FROM tenants WHERE slug = 'default';

    -- case_types
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'case_types' AND column_name = 'tenant_id') THEN
        ALTER TABLE case_types ADD COLUMN tenant_id UUID REFERENCES tenants(id);
        UPDATE case_types SET tenant_id = default_tenant_id WHERE tenant_id IS NULL;
        CREATE INDEX idx_case_types_tenant ON case_types(tenant_id);
    END IF;

    -- case_instances
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'case_instances' AND column_name = 'tenant_id') THEN
        ALTER TABLE case_instances ADD COLUMN tenant_id UUID REFERENCES tenants(id);
        UPDATE case_instances SET tenant_id = default_tenant_id WHERE tenant_id IS NULL;
        CREATE INDEX idx_cases_tenant ON case_instances(tenant_id);
    END IF;

    -- work_queues
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'work_queues' AND column_name = 'tenant_id') THEN
        ALTER TABLE work_queues ADD COLUMN tenant_id UUID REFERENCES tenants(id);
        UPDATE work_queues SET tenant_id = default_tenant_id WHERE tenant_id IS NULL;
        CREATE INDEX idx_queues_tenant ON work_queues(tenant_id);
    END IF;

    -- form_definitions
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'form_definitions' AND column_name = 'tenant_id') THEN
        ALTER TABLE form_definitions ADD COLUMN tenant_id UUID REFERENCES tenants(id);
        UPDATE form_definitions SET tenant_id = default_tenant_id WHERE tenant_id IS NULL;
        CREATE INDEX idx_forms_tenant ON form_definitions(tenant_id);
    END IF;

    -- rule_definitions
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'rule_definitions' AND column_name = 'tenant_id') THEN
        ALTER TABLE rule_definitions ADD COLUMN tenant_id UUID REFERENCES tenants(id);
        UPDATE rule_definitions SET tenant_id = default_tenant_id WHERE tenant_id IS NULL;
        CREATE INDEX idx_rules_tenant ON rule_definitions(tenant_id);
    END IF;

    -- data_models
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'data_models' AND column_name = 'tenant_id') THEN
        ALTER TABLE data_models ADD COLUMN tenant_id UUID REFERENCES tenants(id);
        UPDATE data_models SET tenant_id = default_tenant_id WHERE tenant_id IS NULL;
        CREATE INDEX idx_data_models_tenant ON data_models(tenant_id);
    END IF;
END $$;

COMMIT;
