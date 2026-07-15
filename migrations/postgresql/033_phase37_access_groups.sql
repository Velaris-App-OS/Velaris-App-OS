-- P37: Operator & Access Group Model
-- Creates: portals, access_roles, access_groups, operator_access_groups
-- Extends: user_directory (current_access_group_id)
-- Seeds:   3 system portals, 3 base access roles, 1 default admin group

BEGIN;

-- ── 1. portals ────────────────────────────────────────────────────────────────
CREATE TABLE portals (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100)  NOT NULL,
    portal_type VARCHAR(50)   NOT NULL DEFAULT 'staff',   -- staff|customer|manager|admin
    modules     JSONB         NOT NULL DEFAULT '[]',       -- sidebar module keys
    homepage    VARCHAR(100)  NOT NULL DEFAULT '/work-center',
    theme       JSONB         NOT NULL DEFAULT '{}',       -- brand_color, logo_text, etc.
    tenant_id   VARCHAR(255)  NULL,                        -- NULL = system-wide default
    is_active   BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT portals_type_check CHECK (
        portal_type IN ('staff','customer','manager','admin','mobile')
    )
);

CREATE INDEX idx_portals_tenant  ON portals (tenant_id) WHERE tenant_id IS NOT NULL;
CREATE INDEX idx_portals_type    ON portals (portal_type);

-- ── 2. access_roles ───────────────────────────────────────────────────────────
CREATE TABLE access_roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100)  NOT NULL,
    description TEXT          NOT NULL DEFAULT '',
    -- Array of privilege objects matching ABAC engine format in access_control.py:
    -- [{"resource":"case","case_type_id":"*","actions":["create","read","resolve"]}]
    privileges  JSONB         NOT NULL DEFAULT '[]',
    tenant_id   VARCHAR(255)  NULL,    -- NULL = system-wide built-in role
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT access_roles_name_tenant_uq UNIQUE (name, tenant_id)
);

CREATE INDEX idx_access_roles_tenant ON access_roles (tenant_id) WHERE tenant_id IS NOT NULL;

-- ── 3. access_groups ──────────────────────────────────────────────────────────
CREATE TABLE access_groups (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                  VARCHAR(100)  NOT NULL,
    description           TEXT          NOT NULL DEFAULT '',
    tenant_id             VARCHAR(255)  NOT NULL,          -- always tenant-scoped
    portal_id             UUID          NOT NULL REFERENCES portals(id) ON DELETE RESTRICT,
    role_ids              JSONB         NOT NULL DEFAULT '[]',  -- [uuid, ...]
    allowed_case_type_ids JSONB         NOT NULL DEFAULT '["*"]',
    allowed_queue_ids     JSONB         NOT NULL DEFAULT '["*"]',
    is_default            BOOLEAN       NOT NULL DEFAULT FALSE, -- auto-assign new operators
    is_active             BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT access_groups_name_tenant_uq UNIQUE (name, tenant_id)
);

CREATE INDEX idx_access_groups_tenant   ON access_groups (tenant_id);
CREATE INDEX idx_access_groups_portal   ON access_groups (portal_id);
CREATE INDEX idx_access_groups_default  ON access_groups (tenant_id, is_default)
    WHERE is_default IS TRUE;

-- ── 4. operator_access_groups ─────────────────────────────────────────────────
CREATE TABLE operator_access_groups (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id      VARCHAR(255) NOT NULL,   -- matches user_directory.user_id
    access_group_id  UUID         NOT NULL REFERENCES access_groups(id) ON DELETE CASCADE,
    is_primary       BOOLEAN      NOT NULL DEFAULT FALSE,
    assigned_by      VARCHAR(255) NULL,
    assigned_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT oag_operator_group_uq UNIQUE (operator_id, access_group_id)
);

CREATE INDEX idx_oag_operator       ON operator_access_groups (operator_id);
CREATE INDEX idx_oag_access_group   ON operator_access_groups (access_group_id);
CREATE INDEX idx_oag_primary        ON operator_access_groups (operator_id, is_primary)
    WHERE is_primary IS TRUE;

-- ── 5. extend user_directory ──────────────────────────────────────────────────
-- Stores the operator's currently active access group (persists across sessions).
-- NULL = not yet resolved; resolved on first login using primary group.
ALTER TABLE user_directory
    ADD COLUMN IF NOT EXISTS current_access_group_id UUID
        REFERENCES access_groups(id) ON DELETE SET NULL;

CREATE INDEX idx_user_dir_current_ag
    ON user_directory (current_access_group_id)
    WHERE current_access_group_id IS NOT NULL;

-- ── 6. seed: system portals ───────────────────────────────────────────────────
INSERT INTO portals (id, name, portal_type, modules, homepage, theme) VALUES
(
    '00000000-0000-0000-0000-000000000001',
    'Staff Studio',
    'staff',
    '["dashboard","work-center","case-manager","analytics","process-mining",
      "hxnexus","portal-admin","email-admin","push-admin","admin","tenants","scout"]',
    '/work-center',
    '{}'
),
(
    '00000000-0000-0000-0000-000000000002',
    'Manager Portal',
    'manager',
    '["dashboard","analytics","process-mining","work-center","hxnexus","admin"]',
    '/analytics',
    '{}'
),
(
    '00000000-0000-0000-0000-000000000003',
    'Admin Console',
    'admin',
    '["admin","tenants","user-directory","access-groups","portal-admin","email-admin",
      "push-admin","analytics","scout","hxnexus"]',
    '/admin',
    '{}'
);

-- ── 7. seed: system access roles ─────────────────────────────────────────────
-- Names match existing require_role() strings so AuthenticatedUser.roles keeps working.
INSERT INTO access_roles (id, name, description, privileges) VALUES
(
    '00000000-0000-0001-0000-000000000001',
    'admin',
    'Full system access — all resources, all actions',
    '[{"resource":"*","case_type_id":"*","actions":["*"]}]'
),
(
    '00000000-0000-0001-0000-000000000002',
    'staff',
    'Standard case worker — create, read and work cases',
    '[{"resource":"case","case_type_id":"*","actions":["create","read","update","assign"]},
      {"resource":"case_type","actions":["read"]},
      {"resource":"document","actions":["read","upload"]}]'
),
(
    '00000000-0000-0001-0000-000000000003',
    'viewer',
    'Read-only access across all case types',
    '[{"resource":"case","case_type_id":"*","actions":["read"]},
      {"resource":"case_type","actions":["read"]}]'
);

-- ── 8. seed: default Administrators group (system tenant) ─────────────────────
-- tenant_id 'system' is the built-in tenant for dev/bootstrap.
-- Real tenant admins will create their own groups via the API.
INSERT INTO access_groups (
    id, name, description, tenant_id, portal_id,
    role_ids, allowed_case_type_ids, allowed_queue_ids, is_default
) VALUES (
    '00000000-0000-0002-0000-000000000001',
    'Administrators',
    'Default admin group — full access to all modules',
    'system',
    '00000000-0000-0000-0000-000000000001',   -- Staff Studio portal
    '["00000000-0000-0001-0000-000000000001"]', -- admin role
    '["*"]',
    '["*"]',
    TRUE
);

COMMIT;
