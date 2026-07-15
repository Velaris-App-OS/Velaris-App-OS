-- 089: HxGuard Phase B — relationship tuples (Zanzibar shape).
-- case:OBJECT#relation@subject_type:subject_id
-- Written in the SAME transaction as the source mutation (assignment
-- lifecycle, case shares) so authz state cannot diverge from case state.
-- Phase C exports these rows to OpenFGA verbatim.

CREATE TABLE IF NOT EXISTS hxguard_tuples (
    id           UUID         PRIMARY KEY,
    object_type  VARCHAR(30)  NOT NULL,            -- case (more later)
    object_id    UUID         NOT NULL,
    relation     VARCHAR(30)  NOT NULL,            -- assignee | viewer | editor
    subject_type VARCHAR(30)  NOT NULL,            -- user (more later)
    subject_id   VARCHAR(255) NOT NULL,
    created_by   VARCHAR(255),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hxguard_tuple UNIQUE (object_type, object_id, relation, subject_type, subject_id)
);

CREATE INDEX IF NOT EXISTS ix_hxg_tuples_object  ON hxguard_tuples (object_type, object_id);
CREATE INDEX IF NOT EXISTS ix_hxg_tuples_subject ON hxguard_tuples (subject_type, subject_id);
