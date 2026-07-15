-- HELIX Case Management Schema Migration
-- Version: 001
-- Description: Create all case management tables
--
-- Copyright (c) 2024-2025 HELIX Contributors
-- SPDX-License-Identifier: BSL-1.1

BEGIN;

-- ═══════════════════════════════════════════════════════════
-- DESIGN-TIME: Case Type Registry
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS case_types (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(255) NOT NULL,
    version             VARCHAR(50) NOT NULL,
    lifecycle_process_id UUID NOT NULL,
    data_model_id       UUID,
    security_profile_id UUID,
    default_priority    VARCHAR(20) DEFAULT 'medium',
    definition_json     JSONB NOT NULL,
    icon                VARCHAR(100),
    color               VARCHAR(7),
    description         TEXT DEFAULT '',
    tags                TEXT[] DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS case_type_stages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_type_id    UUID NOT NULL REFERENCES case_types(id) ON DELETE CASCADE,
    stage_id        VARCHAR(255) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    stage_type      VARCHAR(50) NOT NULL,
    "order"         INT DEFAULT 0,
    sla_policy_id   VARCHAR(255),
    definition_json JSONB NOT NULL,
    UNIQUE (case_type_id, stage_id)
);

CREATE TABLE IF NOT EXISTS case_type_steps (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_type_id    UUID NOT NULL REFERENCES case_types(id) ON DELETE CASCADE,
    stage_id        UUID NOT NULL REFERENCES case_type_stages(id) ON DELETE CASCADE,
    step_id         VARCHAR(255) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    step_type       VARCHAR(50) NOT NULL,
    bpmn_element_id VARCHAR(255) NOT NULL,
    definition_json JSONB NOT NULL,
    UNIQUE (case_type_id, step_id)
);

-- ═══════════════════════════════════════════════════════════
-- RUNTIME: Case Instances
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS case_instances (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_type_id        UUID NOT NULL REFERENCES case_types(id),
    case_type_version   VARCHAR(50) NOT NULL,
    process_instance_id VARCHAR(255),
    status              VARCHAR(30) NOT NULL DEFAULT 'new',
    priority            VARCHAR(20) NOT NULL DEFAULT 'medium',
    urgency_score       FLOAT DEFAULT 0.0,
    current_stage_id    VARCHAR(255),
    parent_case_id      UUID REFERENCES case_instances(id),
    data                JSONB DEFAULT '{}',
    created_by          VARCHAR(255),
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    metadata            JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_cases_status ON case_instances(status);
CREATE INDEX IF NOT EXISTS idx_cases_type ON case_instances(case_type_id);
CREATE INDEX IF NOT EXISTS idx_cases_priority ON case_instances(priority);
CREATE INDEX IF NOT EXISTS idx_cases_parent ON case_instances(parent_case_id);
CREATE INDEX IF NOT EXISTS idx_cases_urgency ON case_instances(urgency_score DESC);
CREATE INDEX IF NOT EXISTS idx_cases_created ON case_instances(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cases_data ON case_instances USING gin(data);

-- ═══════════════════════════════════════════════════════════
-- ASSIGNMENTS (work items)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS case_assignments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         VARCHAR(255) NOT NULL,
    assignee_type   VARCHAR(20) NOT NULL,
    assignee_id     VARCHAR(255) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    assigned_at     TIMESTAMPTZ DEFAULT now(),
    due_at          TIMESTAMPTZ,
    claimed_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    assigned_by     VARCHAR(255),
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_assignments_assignee
    ON case_assignments(assignee_type, assignee_id);
CREATE INDEX IF NOT EXISTS idx_assignments_case
    ON case_assignments(case_id);
CREATE INDEX IF NOT EXISTS idx_assignments_status
    ON case_assignments(status);
CREATE INDEX IF NOT EXISTS idx_assignments_due
    ON case_assignments(due_at) WHERE status = 'active';

-- ═══════════════════════════════════════════════════════════
-- CASE RELATIONSHIPS
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS case_relationships (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_case_id      UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    target_case_id      UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    relationship_type   VARCHAR(30) NOT NULL,
    propagate_status    BOOLEAN DEFAULT false,
    propagate_priority  BOOLEAN DEFAULT false,
    required            BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (source_case_id, target_case_id, relationship_type)
);

-- ═══════════════════════════════════════════════════════════
-- SLA TRACKING
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS case_sla_instances (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    sla_policy_id           VARCHAR(255) NOT NULL,
    target_id               VARCHAR(255) NOT NULL,
    status                  VARCHAR(20) NOT NULL DEFAULT 'on_track',
    started_at              TIMESTAMPTZ NOT NULL,
    goal_at                 TIMESTAMPTZ NOT NULL,
    deadline_at             TIMESTAMPTZ NOT NULL,
    paused_at               TIMESTAMPTZ,
    paused_duration_seconds INT DEFAULT 0,
    breached_at             TIMESTAMPTZ,
    metadata                JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sla_case
    ON case_sla_instances(case_id);
CREATE INDEX IF NOT EXISTS idx_sla_deadline
    ON case_sla_instances(deadline_at) WHERE status IN ('on_track', 'at_risk');

-- ═══════════════════════════════════════════════════════════
-- AUDIT TRAIL (append-only)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS case_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID NOT NULL REFERENCES case_instances(id),
    action          VARCHAR(100) NOT NULL,
    actor_id        VARCHAR(255),
    actor_type      VARCHAR(20) DEFAULT 'user',
    timestamp       TIMESTAMPTZ DEFAULT now(),
    details         JSONB DEFAULT '{}',
    previous_value  JSONB,
    new_value       JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_case
    ON case_audit_log(case_id, timestamp DESC);

-- ═══════════════════════════════════════════════════════════
-- WORK QUEUES
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS work_queues (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT DEFAULT '',
    filter_criteria JSONB DEFAULT '{}',
    sort_fields     TEXT[] DEFAULT '{"urgency"}',
    sort_ascending  BOOLEAN DEFAULT true,
    visible_to_roles TEXT[] DEFAULT '{}',
    auto_assignment BOOLEAN DEFAULT false,
    urgency_formula TEXT,
    max_items       INT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════
-- DESIGN-TIME ARTIFACT STORAGE
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS data_models (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    version         VARCHAR(50) NOT NULL,
    definition_json JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS form_definitions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    version         VARCHAR(50) NOT NULL,
    data_model_id   UUID REFERENCES data_models(id),
    definition_json JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS rule_definitions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    version         VARCHAR(50) NOT NULL,
    rule_type       VARCHAR(50) NOT NULL,
    scope           VARCHAR(30) DEFAULT 'global',
    scope_target_id VARCHAR(255),
    definition_json JSONB NOT NULL,
    enabled         BOOLEAN DEFAULT true,
    priority        INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (name, version)
);

COMMIT;
