-- 092: Marketplace conformance gate (#27 Phase C).
-- Records each workspace's structural-conformance state so submission can be
-- hard-gated and so the grandfather window can badge pre-gate packages.
--   none              — never run
--   legacy_unverified — predates HxTest; grandfathered (60-day window to submit)
--   unverified        — window elapsed, no passing result
--   structural_passed — 100% structural conformance (submittable)
--   full_passed       — structural + scenario (advisory) both passed
--
-- Guarded: marketplace_workspaces is created only when the marketplace feature
-- is activated, so this migration is a no-op until then (it must not abort the
-- start-velaris.sh migration step, which halts on any error). When the table is
-- later created from the model it already carries these columns; this ALTER only
-- retrofits an instance that already had marketplace data before the gate shipped.

DO $$
BEGIN
  IF to_regclass('public.marketplace_workspaces') IS NOT NULL THEN
    ALTER TABLE marketplace_workspaces
        ADD COLUMN IF NOT EXISTS conformance_status     VARCHAR(30) NOT NULL DEFAULT 'none';
    ALTER TABLE marketplace_workspaces
        ADD COLUMN IF NOT EXISTS conformance_run_id     UUID NULL;
    ALTER TABLE marketplace_workspaces
        ADD COLUMN IF NOT EXISTS conformance_checked_at TIMESTAMPTZ NULL;

    -- Grandfather: workspaces already submitted/approved before the gate shipped
    -- are badged legacy_unverified, never blocked.
    UPDATE marketplace_workspaces
       SET conformance_status = 'legacy_unverified'
     WHERE conformance_status = 'none'
       AND status IN ('submitted', 'approved');
  END IF;
END $$;
