-- Marketplace Layer-1 P2: templated-connector drift detection.
--
-- A publisher-controlled schema change must never silently reshape an
-- integration. When an updated package's descriptor hash differs from the
-- grant's, the new mapping is regenerated and CLASSIFIED:
--   additive (within the granted envelope, official tier only) > auto-apply, logged
--   breaking or capability-widening (or ANY community-tier change) > held in
--   `proposed` with status pending_reapproval — the OLD mapping keeps running
--   until an admin approves or rejects the drift.

ALTER TABLE marketplace_capability_grants
    ADD COLUMN IF NOT EXISTS proposed JSONB;
