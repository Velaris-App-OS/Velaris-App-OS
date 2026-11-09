-- 225 — reference tables (Form Builder v2 step 3, 2026-10-29).
--
-- Admin-managed lists a form reads: branches, products, price bands. The same
-- table feeds dropdowns, search boxes and cascading options, and fills other
-- fields from the chosen row. A tenant's table is its own; a NULL tenant is a
-- platform table (read by all, changed by the operator only).
--
-- Additive and re-runnable; the previous version's code never reads them.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS reference_tables (
    id          UUID          PRIMARY KEY,
    tenant_id   UUID          NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_key  VARCHAR(64)   NOT NULL DEFAULT 'platform',
    name        VARCHAR(100)  NOT NULL,
    description TEXT          NOT NULL DEFAULT '',
    columns     JSONB         NOT NULL DEFAULT '[]'::jsonb,
    row_count   INTEGER       NOT NULL DEFAULT 0,
    created_by  VARCHAR(255)  NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reference_tables_tenant_name ON reference_tables (tenant_key, name);
CREATE INDEX IF NOT EXISTS ix_reference_tables_tenant ON reference_tables (tenant_id);

CREATE TABLE IF NOT EXISTS reference_rows (
    id          UUID          PRIMARY KEY,
    table_id    UUID          NOT NULL REFERENCES reference_tables(id) ON DELETE CASCADE,
    sort_order  INTEGER       NOT NULL DEFAULT 0,
    data        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    search_text TEXT          NOT NULL DEFAULT '',
    is_active   BOOLEAN       NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS ix_reference_rows_table ON reference_rows (table_id, sort_order);
