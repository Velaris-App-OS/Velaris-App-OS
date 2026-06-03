-- ============================================================
-- P62 — ENH-12: Story-driven branches
-- Link hxwork_stories to artifact_branches so every story
-- auto-creates a branch and the two lifecycle states stay in sync.
-- ============================================================

ALTER TABLE hxwork_stories
    ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES artifact_branches(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS branch_name TEXT;

CREATE INDEX IF NOT EXISTS ix_hws_branch ON hxwork_stories(branch_id);
