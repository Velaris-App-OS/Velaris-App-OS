-- 227 — form drafts: partial answers kept without validation (Form Builder §5 step 7, 2026-10-29).
--
-- One draft per person, case and form; see the PostgreSQL file.
-- Additive and re-runnable; the previous version's code never reads it.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS form_drafts (
    case_id      CHAR(36)      NOT NULL,
    form_id      CHAR(36)      NOT NULL,
    user_id      VARCHAR(255)  NOT NULL,
    values_json  JSON          NOT NULL,
    updated_at   DATETIME(6)   NOT NULL,
    PRIMARY KEY (case_id, form_id, user_id),
    KEY ix_form_drafts_user (user_id),
    CONSTRAINT fk_form_drafts_case FOREIGN KEY (case_id) REFERENCES case_instances(id) ON DELETE CASCADE
);
