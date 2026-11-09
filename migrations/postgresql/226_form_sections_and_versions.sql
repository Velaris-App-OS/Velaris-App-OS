-- 226 — reusable form sections and form versions per case (Form Builder §5 step 6, 2026-10-29).
--
-- form_sections: an "Address" or "Bank details" section built once and linked
-- from many forms. A form keeps a link and a copy of the fields; saving the
-- section updates every form that links it. A NULL tenant is a platform
-- section (read by all, changed by the operator only).
--
-- form_snapshots + case_form_versions: a form with
-- `settings.versioning = "per_case"` is pinned for each case the first time
-- the case uses it; the case keeps reading that snapshot. A snapshot is never
-- changed and is kept when its form is deleted, so a pinned case still works.
--
-- Additive and re-runnable; the previous version's code never reads them, and
-- a form without the setting behaves exactly as before.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS form_sections (
    id           UUID          PRIMARY KEY,
    tenant_id    UUID          NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_key   VARCHAR(64)   NOT NULL DEFAULT 'platform',
    name         VARCHAR(100)  NOT NULL,
    description  TEXT          NOT NULL DEFAULT '',
    version      INTEGER       NOT NULL DEFAULT 1,
    section_json JSONB         NOT NULL DEFAULT '{}'::jsonb,
    updated_by   VARCHAR(255)  NULL,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_form_sections_tenant_name ON form_sections (tenant_key, name);
CREATE INDEX IF NOT EXISTS ix_form_sections_tenant ON form_sections (tenant_id);

CREATE TABLE IF NOT EXISTS form_snapshots (
    id              UUID         PRIMARY KEY,
    form_id         UUID         NOT NULL,
    tenant_id       UUID         NULL,
    content_hash    CHAR(64)     NOT NULL,
    name            VARCHAR(255) NOT NULL DEFAULT '',
    definition_json JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_form_snapshots_form_hash ON form_snapshots (form_id, content_hash);

CREATE TABLE IF NOT EXISTS case_form_versions (
    case_id      UUID         NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    form_id      UUID         NOT NULL,
    snapshot_id  UUID         NOT NULL REFERENCES form_snapshots(id),
    pinned_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (case_id, form_id)
);
