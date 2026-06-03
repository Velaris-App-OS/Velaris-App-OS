-- HELIX P34b — User directory + SLA policy tree link
CREATE TABLE IF NOT EXISTS user_directory (
    id               UUID PRIMARY KEY,
    user_id          VARCHAR(255) NOT NULL UNIQUE,
    email            VARCHAR(255),
    display_name     VARCHAR(255),
    manager_user_id  VARCHAR(255),
    access_group_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    roles            JSONB NOT NULL DEFAULT '[]'::jsonb,
    timezone         VARCHAR(64) NOT NULL DEFAULT 'UTC',
    tenant_id        VARCHAR(64),
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_directory_manager ON user_directory(manager_user_id);
CREATE INDEX IF NOT EXISTS idx_user_directory_tenant  ON user_directory(tenant_id);
