-- 227 — form drafts: partial answers kept without validation (Form Builder §5 step 7, 2026-10-29).
--
-- One draft per person, case and form. A draft is the person's own: nobody
-- else reads it, it is never checked, and it is removed when that person
-- submits the form (or completes the step) for the case, or when the case is
-- deleted. Values are limited to the form's keys the person may change.
--
-- Additive and re-runnable; the previous version's code never reads it.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS form_drafts (
    case_id      UUID          NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    form_id      UUID          NOT NULL,
    user_id      VARCHAR(255)  NOT NULL,
    values_json  JSONB         NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (case_id, form_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_form_drafts_user ON form_drafts (user_id);
