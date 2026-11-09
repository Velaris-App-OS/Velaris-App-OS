-- 224 — the deletion ledger's outbox (Nov_TODO "User deletion v2" part b).
--
-- A purged account must stay purged after a restore: a backup taken before the
-- purge brings it back, with its access. The ledger that prevents that lives
-- OUTSIDE the database (a restore rolls the database back) — signed files in
-- .deletions/, copied off the box with the backups and to the standby, replayed
-- by restore-velaris.sh. This table is only the way there: the record is queued
-- here in the SAME transaction as the purge, so a purge never lacks its record
-- and a record never names an account that was not purged; the file is written
-- right after and the hourly job retries any that failed (written_at NULL).
--
-- Additive and re-runnable; the previous version's code never reads it.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS deletion_ledger (
    id          UUID         PRIMARY KEY,
    account_id  UUID         NOT NULL,
    record      TEXT         NOT NULL,          -- the signed record, exactly as written to the file
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    written_at  TIMESTAMPTZ  NULL               -- NULL = file not written yet (the job retries)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_deletion_ledger_account ON deletion_ledger (account_id);
CREATE INDEX IF NOT EXISTS ix_deletion_ledger_unwritten ON deletion_ledger (written_at);
