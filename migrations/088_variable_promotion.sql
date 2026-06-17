-- 088: Case Variables Phase 4 — case.data promotion (migration wizard).
-- promoted_source records which legacy blob key a variable definition was
-- promoted from. The case_vars read façade suppresses that blob key for the
-- case type (typed row becomes the source of truth); the case.data column
-- itself is never modified, so direct blob consumers and rollback stay safe.
-- Un-promoting = deleting the definition (and its instance rows).

ALTER TABLE case_type_variables
    ADD COLUMN IF NOT EXISTS promoted_source VARCHAR(255);

-- partial index: the façade looks up promoted keys per case type on every read
CREATE INDEX IF NOT EXISTS ix_ctv_promoted
    ON case_type_variables (case_type_id, promoted_source)
    WHERE promoted_source IS NOT NULL;
