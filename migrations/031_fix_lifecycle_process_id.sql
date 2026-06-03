-- Migration 031 — Make lifecycle_process_id optional and TEXT
-- The column was originally UUID NOT NULL, which:
--   (a) blocks NLP-generated case types that use a string placeholder
--   (b) forces users to have a deployed BPMN process before creating a case type
--
-- Changing to TEXT NULL keeps existing UUID values intact and allows
-- both UUID strings and free-form identifiers.

ALTER TABLE case_types
  ALTER COLUMN lifecycle_process_id TYPE TEXT USING lifecycle_process_id::TEXT,
  ALTER COLUMN lifecycle_process_id DROP NOT NULL;
