-- Migration 080: Change scheduled_releases.enabled from BOOLEAN to VARCHAR(20)
--
-- Before: enabled BOOLEAN NOT NULL DEFAULT FALSE
-- After:  enabled VARCHAR(20) NULL
--
-- Semantics:
--   NULL          = feature not yet enabled
--   "v1.0.0" etc  = feature enabled at this version
--
-- Existing rows that were enabled=TRUE keep their version value (from the
-- version column if set, or "released" as a safe fallback).
-- Existing rows that were enabled=FALSE become NULL.

ALTER TABLE scheduled_releases
  ALTER COLUMN enabled TYPE VARCHAR(20)
  USING (CASE WHEN enabled THEN COALESCE(version, 'released') ELSE NULL END);

ALTER TABLE scheduled_releases ALTER COLUMN enabled DROP NOT NULL;
ALTER TABLE scheduled_releases ALTER COLUMN enabled DROP DEFAULT;
