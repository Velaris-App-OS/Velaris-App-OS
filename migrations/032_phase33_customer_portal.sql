-- Migration 032 — P33 Customer Portal
-- Adds portal tracking columns to case_instances and portal_enabled flag to case_types.
-- Tenant branding is stored in tenants.settings JSON (no new table needed).

ALTER TABLE case_instances
  ADD COLUMN IF NOT EXISTS portal_tracking_token  UUID         UNIQUE,
  ADD COLUMN IF NOT EXISTS portal_submitter_name  VARCHAR(255),
  ADD COLUMN IF NOT EXISTS portal_submitter_email VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_cases_portal_token ON case_instances(portal_tracking_token)
  WHERE portal_tracking_token IS NOT NULL;

ALTER TABLE case_types
  ADD COLUMN IF NOT EXISTS portal_enabled BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_case_types_portal ON case_types(portal_enabled)
  WHERE portal_enabled = true;
