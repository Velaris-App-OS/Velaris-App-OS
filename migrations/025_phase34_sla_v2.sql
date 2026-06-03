-- HELIX P34 — SLA v2 + Escalation Trees

-- Extend case_sla_instances
ALTER TABLE case_sla_instances
    ADD COLUMN IF NOT EXISTS pause_reason            VARCHAR(255),
    ADD COLUMN IF NOT EXISTS pause_reasons_log       JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS escalation_level        INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS escalation_tree_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS escalation_history      JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS business_calendar_id    UUID;

CREATE INDEX IF NOT EXISTS idx_sla_escalation_level ON case_sla_instances(escalation_level);

-- Escalation tree table
CREATE TABLE IF NOT EXISTS escalation_trees (
    id             UUID PRIMARY KEY,
    name           VARCHAR(255) NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    scope          VARCHAR(32) NOT NULL DEFAULT 'global',
    case_type_id   UUID REFERENCES case_types(id),
    tenant_id      VARCHAR(64),
    tree_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_by     VARCHAR(255),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_escalation_trees_scope     ON escalation_trees(scope);
CREATE INDEX IF NOT EXISTS idx_escalation_trees_case_type ON escalation_trees(case_type_id);
CREATE INDEX IF NOT EXISTS idx_escalation_trees_tenant    ON escalation_trees(tenant_id);
