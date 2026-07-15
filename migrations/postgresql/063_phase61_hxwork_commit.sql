-- ============================================================
-- P61 — HxWork redesign + platform-wide Commit pattern
-- Migration 063
-- ============================================================

-- Platform-wide commit audit trail
CREATE TABLE IF NOT EXISTS component_commits (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    component_type   TEXT        NOT NULL,
    component_id     TEXT        NOT NULL,
    component_name   TEXT        NOT NULL DEFAULT '',
    commit_message   TEXT        NOT NULL,
    committed_by     TEXT        NOT NULL,
    diff_snapshot    JSONB,
    story_matches    JSONB,      -- [{story_id, title, from_status, to_status}]
    committed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_cc_component ON component_commits(component_type, component_id);
CREATE INDEX IF NOT EXISTS ix_cc_committed ON component_commits(committed_at DESC);
CREATE INDEX IF NOT EXISTS ix_cc_user      ON component_commits(committed_by);

-- Extend boards with artifact scope (replaces case_type_id semantics)
ALTER TABLE hxwork_boards
    ADD COLUMN IF NOT EXISTS artifact_type TEXT,
    ADD COLUMN IF NOT EXISTS artifact_id   TEXT;

-- User stories — replaces hxwork_sprint_cards (case-based cards gone)
CREATE TABLE IF NOT EXISTS hxwork_stories (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id            UUID        NOT NULL REFERENCES hxwork_boards(id) ON DELETE CASCADE,
    sprint_id           UUID        REFERENCES hxwork_sprints(id) ON DELETE SET NULL,
    title               TEXT        NOT NULL,
    description         TEXT,
    acceptance_criteria TEXT,
    status              TEXT        NOT NULL DEFAULT 'backlog',
    story_points        INTEGER,
    assigned_to         TEXT,
    linked_commit_ids   JSONB       NOT NULL DEFAULT '[]',
    created_by          TEXT        NOT NULL DEFAULT 'system',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hws_board  ON hxwork_stories(board_id);
CREATE INDEX IF NOT EXISTS ix_hws_sprint ON hxwork_stories(sprint_id);
CREATE INDEX IF NOT EXISTS ix_hws_status ON hxwork_stories(status);

-- Story-to-story relations (preserved, repurposed from case-based relations)
CREATE TABLE IF NOT EXISTS hxwork_story_relations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id    UUID NOT NULL REFERENCES hxwork_boards(id) ON DELETE CASCADE,
    from_story  UUID NOT NULL REFERENCES hxwork_stories(id) ON DELETE CASCADE,
    to_story    UUID NOT NULL REFERENCES hxwork_stories(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL DEFAULT 'blocks'
);
CREATE INDEX IF NOT EXISTS ix_hws_rel_board ON hxwork_story_relations(board_id);
