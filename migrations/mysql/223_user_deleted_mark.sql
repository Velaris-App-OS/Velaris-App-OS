-- 223 — a deleted account: marked, refused at once, purged after a grace period.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/223. See that file
-- for why; additive — five nullable columns and an index.
--
-- Dialect notes:
--  * TIMESTAMPTZ → DATETIME(6).
--  * `ADD COLUMN` is plain, matching 207/206: MySQL 8.4 has no
--    `ADD COLUMN IF NOT EXISTS`, so once-only application is the runner's ledger.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE velaris_users
    ADD COLUMN deleted_at   DATETIME(6)  NULL,
    ADD COLUMN deleted_by   VARCHAR(255) NULL,
    ADD COLUMN purge_after  DATETIME(6)  NULL,
    ADD COLUMN deleted_meta TEXT         NULL,
    ADD COLUMN tokens_valid_after DATETIME(6) NULL,
    ADD INDEX ix_velaris_users_purge_after (purge_after);
