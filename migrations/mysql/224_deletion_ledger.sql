-- 224 — the deletion ledger's outbox. MySQL 8.4 / MariaDB 11.8 sibling of
-- migrations/postgresql/224. See that file for why; additive — one table.
--
-- Dialect notes:
--  * UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6) with NO DEFAULT (MySQL 8.4
--    rejects `DEFAULT now()` there — migration 176): the application writes it.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS deletion_ledger (
    id          CHAR(36)     PRIMARY KEY,
    account_id  CHAR(36)     NOT NULL,
    record      TEXT         NOT NULL,
    created_at  DATETIME(6)  NOT NULL,
    written_at  DATETIME(6)  NULL,
    UNIQUE KEY ux_deletion_ledger_account (account_id),
    KEY ix_deletion_ledger_unwritten (written_at)
);
