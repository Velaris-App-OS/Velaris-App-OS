-- Phase 38c: Pega-style case locking on assignments
-- When an operator opens their task, the assignment is locked to them.
-- No other operator can submit the same step while the lock is held.
-- Lock auto-expires (default 30 min); released on submit or explicit unlock.

ALTER TABLE case_assignments
    ADD COLUMN IF NOT EXISTS locked_by    VARCHAR(255)  NULL,
    ADD COLUMN IF NOT EXISTS locked_at    TIMESTAMPTZ   NULL,
    ADD COLUMN IF NOT EXISTS lock_expires_at TIMESTAMPTZ NULL;

-- Fast lookup: "is this step locked by someone active?"
CREATE INDEX IF NOT EXISTS idx_assignments_lock
    ON case_assignments (case_id, step_id, locked_by)
    WHERE status = 'active';
