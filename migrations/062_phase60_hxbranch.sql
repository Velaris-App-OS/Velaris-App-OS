-- ============================================================
-- P60 — HxBranch: Artifact Version Control & Live Environment Sync
-- Migration 062
-- ============================================================

-- Extend environment_registry with encrypted API token for live sync
ALTER TABLE environment_registry
    ADD COLUMN IF NOT EXISTS api_token_enc       JSONB,
    ADD COLUMN IF NOT EXISTS connection_verified_at TIMESTAMPTZ;

-- Artifact branches — one row per branch (app or artifact level)
CREATE TABLE IF NOT EXISTS artifact_branches (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT        NOT NULL,
    description       TEXT,
    branch_type       TEXT        NOT NULL DEFAULT 'artifact',   -- 'app' | 'artifact'
    artifact_type     TEXT,       -- 'case_type'|'form'|'integration'|'rule' — NULL for app-level
    artifact_id       TEXT,       -- remote artifact UUID/slug as text
    app_package_id    UUID        REFERENCES app_packages(id) ON DELETE SET NULL,
    source_env_id     UUID        REFERENCES environment_registry(id) ON DELETE SET NULL,
    source_env_name   TEXT        NOT NULL DEFAULT 'unknown',
    status            TEXT        NOT NULL DEFAULT 'open',
    -- 'open' | 'pending_review' | 'approved' | 'merged' | 'rejected' | 'closed'
    content_snapshot  JSONB       NOT NULL DEFAULT '{}',   -- artifact data as pulled from remote
    base_snapshot     JSONB       NOT NULL DEFAULT '{}',   -- local main state at pull time
    conflict_detected BOOLEAN     NOT NULL DEFAULT FALSE,
    merge_diff        JSONB,                               -- populated after merge
    created_by        TEXT        NOT NULL DEFAULT 'system',
    reviewed_by       TEXT,
    merged_by         TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    merged_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_artifact_branches_status     ON artifact_branches(status);
CREATE INDEX IF NOT EXISTS ix_artifact_branches_source_env ON artifact_branches(source_env_id);
CREATE INDEX IF NOT EXISTS ix_artifact_branches_type       ON artifact_branches(branch_type, artifact_type);
CREATE INDEX IF NOT EXISTS ix_artifact_branches_created    ON artifact_branches(created_at DESC);

-- Branch reviews — one per reviewer decision
CREATE TABLE IF NOT EXISTS branch_reviews (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id   UUID        NOT NULL REFERENCES artifact_branches(id) ON DELETE CASCADE,
    reviewer_id TEXT        NOT NULL,
    decision    TEXT        NOT NULL,   -- 'approved' | 'rejected' | 'changes_requested'
    comments    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_branch_reviews_branch ON branch_reviews(branch_id);
CREATE INDEX IF NOT EXISTS ix_branch_reviews_created ON branch_reviews(created_at DESC);
