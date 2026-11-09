-- 231 — security_events.repeat_count (2026-11-05). MySQL 8.4 / MariaDB 11.8
-- sibling of migrations/postgresql/231; see that file for why.
--
-- Dialect notes:
--  * A NOT NULL column with a constant default, added at the end of the table,
--    is INSTANT on MySQL 8.4 and MariaDB 11.8: no rebuild, writes continue.
--    Stated explicitly so a server that could not do it instantly refuses
--    instead of rebuilding the table.
--  * INSTANT still needs a brief exclusive metadata lock. Behind a long
--    transaction on the table it would wait (default: a year) and every audit
--    write would queue behind it. lock_wait_timeout = 2 s caps that: if the
--    table is busy the migration stops with a lock-wait error, nothing changed,
--    and the update can be run again.
--  * MySQL 8.4 has no IF NOT EXISTS for ADD COLUMN: the runner's ledger applies
--    this file once.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

SET SESSION lock_wait_timeout = 2;
ALTER TABLE security_events ADD COLUMN repeat_count INT NOT NULL DEFAULT 1, ALGORITHM=INSTANT;
