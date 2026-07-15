BEGIN;

-- Prevent duplicate active assignments for the same case+step
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_assignment
    ON case_assignments(case_id, step_id)
    WHERE status = 'active';

-- Prevent duplicate SLA instances for the same case+policy
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_sla
    ON case_sla_instances(case_id, sla_policy_id)
    WHERE status IN ('on_track', 'at_risk', 'paused');

-- Prevent duplicate form submissions (one completed assignment per step per case)
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_completed_assignment
    ON case_assignments(case_id, step_id)
    WHERE status = 'completed';

COMMIT;
