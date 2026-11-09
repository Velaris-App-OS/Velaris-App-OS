-- 230 — security_events.tenant_id (2026-11-04). MySQL 8.4 / MariaDB 11.8
-- sibling of migrations/postgresql/230; see that file for why.
--
-- Dialect notes:
--  * ADD COLUMN (nullable, no default) and the secondary index run online
--    (ALGORITHM=INPLACE, LOCK=NONE): writes continue. Stated explicitly so a
--    server that could not do it online refuses instead of locking the table.
--  * MySQL 8.4 has no IF NOT EXISTS for these: the runner's ledger applies this
--    file once.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE security_events ADD COLUMN tenant_id VARCHAR(255) NULL, ALGORITHM=INPLACE, LOCK=NONE;
CREATE INDEX idx_security_events_tenant ON security_events (tenant_id, timestamp) ALGORITHM=INPLACE LOCK=NONE;
