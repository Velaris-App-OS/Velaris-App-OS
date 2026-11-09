-- 225 — reference tables. MySQL 8.4 / MariaDB 11.8 sibling of
-- migrations/postgresql/225. See that file for why; additive — two tables.
--
-- Dialect notes:
--  * UUID → CHAR(36); JSONB → JSON; TIMESTAMPTZ → DATETIME(6) with NO DEFAULT
--    (MySQL 8.4 rejects `DEFAULT now()` there — migration 176): the
--    application writes it. TEXT and JSON take no DEFAULT either.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS reference_tables (
    id          CHAR(36)      PRIMARY KEY,
    tenant_id   CHAR(36)      NULL,
    tenant_key  VARCHAR(64)   NOT NULL DEFAULT 'platform',
    name        VARCHAR(100)  NOT NULL,
    description TEXT          NOT NULL,
    columns     JSON          NOT NULL,
    row_count   INT           NOT NULL DEFAULT 0,
    created_by  VARCHAR(255)  NULL,
    created_at  DATETIME(6)   NOT NULL,
    updated_at  DATETIME(6)   NOT NULL,
    UNIQUE KEY ux_reference_tables_tenant_name (tenant_key, name),
    KEY ix_reference_tables_tenant (tenant_id),
    CONSTRAINT fk_reference_tables_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reference_rows (
    id          CHAR(36)      PRIMARY KEY,
    table_id    CHAR(36)      NOT NULL,
    sort_order  INT           NOT NULL DEFAULT 0,
    data        JSON          NOT NULL,
    search_text TEXT          NOT NULL,
    is_active   TINYINT(1)    NOT NULL DEFAULT 1,
    KEY ix_reference_rows_table (table_id, sort_order),
    CONSTRAINT fk_reference_rows_table FOREIGN KEY (table_id) REFERENCES reference_tables(id) ON DELETE CASCADE
);
