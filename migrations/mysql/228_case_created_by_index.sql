-- 228 — index case_instances.created_by (R6 soak, 2026-11-02). MySQL 8.4 /
-- MariaDB 11.8 sibling of migrations/postgresql/228; see that file for why.
--
-- Dialect notes:
--  * InnoDB adds a secondary index online (ALGORITHM=INPLACE, LOCK=NONE): writes
--    continue while it builds. Stated explicitly so a server that could not do
--    it online refuses instead of locking the table.
--  * Plain CREATE INDEX (MySQL 8.4 has no IF NOT EXISTS for indexes): the
--    runner's ledger applies this once.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE INDEX idx_cases_created_by ON case_instances (created_by) ALGORITHM=INPLACE LOCK=NONE;
