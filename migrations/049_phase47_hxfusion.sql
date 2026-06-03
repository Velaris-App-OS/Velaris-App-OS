-- P47: HxFusion — Adaptive Execution Engine
-- process_definitions:    deployed BPMN process definitions
-- process_instances:      running (and completed) process instances
-- process_case_bindings:  bidirectional case ↔ process link
-- process_task_log:       per-node execution log

CREATE TABLE process_definitions (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    version         INTEGER      NOT NULL DEFAULT 1,
    description     TEXT,
    bpmn_xml        TEXT         NOT NULL,
    case_type_id    VARCHAR(255),
    status          VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_by      VARCHAR(255),
    tenant_id       VARCHAR(255),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_pd_case_type ON process_definitions (case_type_id);
CREATE INDEX ix_pd_status    ON process_definitions (status);
CREATE UNIQUE INDEX ix_pd_name_ver ON process_definitions (name, version, tenant_id);

CREATE TABLE process_instances (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    definition_id   UUID         NOT NULL REFERENCES process_definitions(id) ON DELETE RESTRICT,
    case_id         UUID,
    status          VARCHAR(20)  NOT NULL DEFAULT 'running',
    current_node    VARCHAR(255),
    context         JSONB        NOT NULL DEFAULT '{}',
    error_node      VARCHAR(255),
    error_message   TEXT,
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    tenant_id       VARCHAR(255)
);

CREATE INDEX ix_pi_definition ON process_instances (definition_id);
CREATE INDEX ix_pi_case       ON process_instances (case_id);
CREATE INDEX ix_pi_status     ON process_instances (status);
CREATE INDEX ix_pi_tenant     ON process_instances (tenant_id);

CREATE TABLE process_case_bindings (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID         NOT NULL,
    instance_id     UUID         NOT NULL REFERENCES process_instances(id) ON DELETE CASCADE,
    binding_type    VARCHAR(30)  NOT NULL DEFAULT 'embedded_subprocess',
    direction       VARCHAR(30)  NOT NULL DEFAULT 'case_to_process',
    status          VARCHAR(20)  NOT NULL DEFAULT 'active',
    stage_id        VARCHAR(255),
    step_id         VARCHAR(255),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX ix_pcb_case     ON process_case_bindings (case_id);
CREATE INDEX ix_pcb_instance ON process_case_bindings (instance_id);
CREATE INDEX ix_pcb_status   ON process_case_bindings (status);

CREATE TABLE process_task_log (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id     UUID         NOT NULL REFERENCES process_instances(id) ON DELETE CASCADE,
    node_id         VARCHAR(255) NOT NULL,
    node_name       VARCHAR(255),
    node_type       VARCHAR(50)  NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'running',
    input_context   JSONB        NOT NULL DEFAULT '{}',
    result          JSONB,
    error           TEXT,
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ
);

CREATE INDEX ix_ptl_instance   ON process_task_log (instance_id);
CREATE INDEX ix_ptl_node_id    ON process_task_log (node_id);
CREATE INDEX ix_ptl_status     ON process_task_log (status);
CREATE INDEX ix_ptl_started    ON process_task_log (started_at DESC);
