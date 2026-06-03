-- P38: Rich Stage/Step Task UI
-- Creates: case_step_completions
-- step_id is a VARCHAR (logical ID from definition_json), not a FK —
-- step definitions are versioned; logical IDs survive version bumps.

BEGIN;

CREATE TABLE case_step_completions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    stage_id        VARCHAR(255) NOT NULL,   -- logical stage id from definition_json
    step_id         VARCHAR(255) NOT NULL,   -- logical step id from definition_json
    step_type       VARCHAR(50)  NOT NULL DEFAULT 'user_task',
    status          VARCHAR(20)  NOT NULL DEFAULT 'completed',  -- completed | rejected
    data            JSONB        NOT NULL DEFAULT '{}',         -- form values / approval reason / doc ref
    completed_by    VARCHAR(255) NULL,
    completed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Upsert key: one completion record per step per case
    CONSTRAINT csc_case_step_uq UNIQUE (case_id, step_id)
);

CREATE INDEX idx_csc_case_id   ON case_step_completions (case_id);
CREATE INDEX idx_csc_stage_id  ON case_step_completions (case_id, stage_id);

COMMIT;
