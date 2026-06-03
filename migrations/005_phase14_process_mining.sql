-- HELIX Phase 14 Migration: Event Log for Process Mining
BEGIN;

CREATE TABLE IF NOT EXISTS case_event_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    case_type_id    UUID NOT NULL REFERENCES case_types(id),
    activity        VARCHAR(255) NOT NULL,    -- step/stage name
    activity_type   VARCHAR(50) NOT NULL,     -- "stage_enter", "stage_exit", "step_start", "step_complete"
    stage_id        VARCHAR(255),
    step_id         VARCHAR(255),
    actor_id        VARCHAR(255),
    actor_type      VARCHAR(20),
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_seconds INT,                      -- populated on exit events
    resource_id     VARCHAR(255),              -- who performed it
    outcome         VARCHAR(50),               -- "success", "failed", "skipped"
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_event_log_case
    ON case_event_log(case_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_event_log_type
    ON case_event_log(case_type_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_event_log_activity
    ON case_event_log(case_type_id, activity);
CREATE INDEX IF NOT EXISTS idx_event_log_timestamp
    ON case_event_log(timestamp);

COMMIT;
