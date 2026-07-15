-- 087: Case Variables Phase 1 (spec v2) — typed, namespaced variable system.
-- Namespaces are first-class registered entities with owner bindings and
-- sensitivity classes; variables are scoped to case types; instance values
-- are EAV rows with FIXED indexes (no dynamic DDL, ever — spec v2 §Security).

CREATE TABLE IF NOT EXISTS variable_namespaces (
    id           UUID         PRIMARY KEY,
    name         VARCHAR(100) NOT NULL UNIQUE,    -- ^[a-z][a-z0-9_]{0,99}$
    owner_type   VARCHAR(20)  NOT NULL,           -- platform | connector | devconn | form | portal | rules
    owner_ref    UUID,                            -- connector_registry.id etc. — the identity binding
    sensitivity  VARCHAR(10)  NOT NULL DEFAULT 'internal',  -- public | internal | pii | secret
    status       VARCHAR(10)  NOT NULL DEFAULT 'active',    -- active | frozen | retired
    created_by   VARCHAR(255),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS namespace_grants (
    id            UUID         PRIMARY KEY,
    namespace_id  UUID         NOT NULL REFERENCES variable_namespaces(id) ON DELETE CASCADE,
    grantee_type  VARCHAR(20)  NOT NULL,          -- connector | devconn | rules | module
    grantee_ref   VARCHAR(255) NOT NULL,
    capability    VARCHAR(10)  NOT NULL,          -- read | write
    granted_by    VARCHAR(255),
    granted_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (namespace_id, grantee_type, grantee_ref, capability)
);

CREATE TABLE IF NOT EXISTS case_type_variables (
    id                   UUID         PRIMARY KEY,
    case_type_id         UUID         NOT NULL REFERENCES case_types(id) ON DELETE CASCADE,
    namespace_id         UUID         NOT NULL REFERENCES variable_namespaces(id),
    name                 VARCHAR(100) NOT NULL,
    full_key             VARCHAR(201) NOT NULL,   -- namespace.name, assembled server-side
    var_type             VARCHAR(20)  NOT NULL DEFAULT 'any',
    definition_status    VARCHAR(12)  NOT NULL DEFAULT 'defined',  -- defined | undeclared | ignored
    sensitivity_override VARCHAR(10),             -- stricter than the namespace only
    label                VARCHAR(255),
    description          TEXT,
    default_value        TEXT,
    required             BOOLEAN      NOT NULL DEFAULT FALSE,
    indexed              BOOLEAN      NOT NULL DEFAULT FALSE,  -- UI filter flag ONLY
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (case_type_id, full_key)
);
CREATE INDEX IF NOT EXISTS ix_ctv_case_type ON case_type_variables (case_type_id);
CREATE INDEX IF NOT EXISTS ix_ctv_status    ON case_type_variables (case_type_id, definition_status);

CREATE TABLE IF NOT EXISTS case_instance_variables (
    id          UUID         PRIMARY KEY,
    case_id     UUID         NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    full_key    VARCHAR(201) NOT NULL,
    value_text  TEXT,
    value_num   DOUBLE PRECISION,
    value_bool  BOOLEAN,
    value_json  JSONB,
    written_by  VARCHAR(255) NOT NULL,            -- resolved server-side, never caller-supplied
    written_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (case_id, full_key)                    -- the upsert target
);
-- Fixed EAV indexes — the entire indexing strategy (spec v2: no dynamic DDL)
CREATE INDEX IF NOT EXISTS ix_civ_key_num  ON case_instance_variables (full_key, value_num);
CREATE INDEX IF NOT EXISTS ix_civ_key_text ON case_instance_variables (full_key, value_text);

-- Reserved namespaces — platform-owned, unclaimable, seeded idempotently
INSERT INTO variable_namespaces (id, name, owner_type, sensitivity, status, created_by)
VALUES
    (gen_random_uuid(), 'velaris', 'platform', 'internal', 'active', 'migration-087'),
    (gen_random_uuid(), 'form',    'form',     'pii',      'active', 'migration-087'),
    (gen_random_uuid(), 'portal',  'portal',   'pii',      'active', 'migration-087'),
    (gen_random_uuid(), 'legacy',  'platform', 'internal', 'active', 'migration-087')
ON CONFLICT (name) DO NOTHING;
