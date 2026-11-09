-- 226 — reusable form sections and form versions per case (Form Builder §5 step 6, 2026-10-29).
-- See the PostgreSQL file for the reasoning. Additive and re-runnable.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS form_sections (
    id           CHAR(36)      PRIMARY KEY,
    tenant_id    CHAR(36)      NULL,
    tenant_key   VARCHAR(64)   NOT NULL DEFAULT 'platform',
    name         VARCHAR(100)  NOT NULL,
    description  TEXT          NOT NULL,
    version      INT           NOT NULL DEFAULT 1,
    section_json JSON          NOT NULL,
    updated_by   VARCHAR(255)  NULL,
    created_at   DATETIME(6)   NOT NULL,
    updated_at   DATETIME(6)   NOT NULL,
    UNIQUE KEY ux_form_sections_tenant_name (tenant_key, name),
    KEY ix_form_sections_tenant (tenant_id),
    CONSTRAINT fk_form_sections_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS form_snapshots (
    id              CHAR(36)     PRIMARY KEY,
    form_id         CHAR(36)     NOT NULL,
    tenant_id       CHAR(36)     NULL,
    content_hash    CHAR(64)     NOT NULL,
    name            VARCHAR(255) NOT NULL DEFAULT '',
    definition_json JSON         NOT NULL,
    created_at      DATETIME(6)  NOT NULL,
    UNIQUE KEY ux_form_snapshots_form_hash (form_id, content_hash)
);

CREATE TABLE IF NOT EXISTS case_form_versions (
    case_id      CHAR(36)     NOT NULL,
    form_id      CHAR(36)     NOT NULL,
    snapshot_id  CHAR(36)     NOT NULL,
    pinned_at    DATETIME(6)  NOT NULL,
    PRIMARY KEY (case_id, form_id),
    CONSTRAINT fk_case_form_versions_case FOREIGN KEY (case_id) REFERENCES case_instances(id) ON DELETE CASCADE,
    CONSTRAINT fk_case_form_versions_snapshot FOREIGN KEY (snapshot_id) REFERENCES form_snapshots(id)
);
