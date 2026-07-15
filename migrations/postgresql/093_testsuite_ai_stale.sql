-- 093: Test Suite (#27) — AI-scenario staleness flag on generated suites.
-- When a case type's definition/rules/integrations/email accounts change, its
-- generated suite's AI scenarios become stale. Structural tests auto-regenerate
-- (deterministic, cheap); AI scenarios are marked stale here and regenerated
-- manually (decision: auto structural / manual AI). hxtest_suites is created by
-- migration 091 (plain CREATE TABLE), so this ALTER is always safe.

ALTER TABLE hxtest_suites ADD COLUMN IF NOT EXISTS ai_stale BOOLEAN NOT NULL DEFAULT false;
