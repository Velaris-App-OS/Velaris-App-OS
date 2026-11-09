-- 223 — a deleted account: marked, refused at once, purged after a grace period
-- (Nov_TODO "User deletion v2", 2026-10-23).
--
-- Until now an admin delete removed only the velaris_users row: the user's
-- tokens kept working until they expired, and their access-group memberships —
-- keyed by username — were inherited by the next account given that name. Now
-- a delete MARKS the row (sign-in refused everywhere, every session ended) and
-- keeps it — the username stays reserved — until `purge_after`, when a job
-- removes it with everything that grants access. An admin can undo in between.
--
-- Additive and re-runnable: five nullable columns, an index. NULL = not
-- deleted, so every existing row reads exactly as before, and the previous
-- version's code (which never reads them) is unaffected while this applies.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE velaris_users ADD COLUMN IF NOT EXISTS deleted_at   TIMESTAMPTZ  NULL;
ALTER TABLE velaris_users ADD COLUMN IF NOT EXISTS deleted_by   VARCHAR(255) NULL;
ALTER TABLE velaris_users ADD COLUMN IF NOT EXISTS purge_after  TIMESTAMPTZ  NULL;
-- What the delete changed and an undo puts back (the directory entry's state).
ALTER TABLE velaris_users ADD COLUMN IF NOT EXISTS deleted_meta TEXT         NULL;
-- Tokens issued before this are refused ("sign out everywhere"): a delete sets
-- it, so an undo does not revive the sessions the delete ended.
ALTER TABLE velaris_users ADD COLUMN IF NOT EXISTS tokens_valid_after TIMESTAMPTZ NULL;
CREATE INDEX IF NOT EXISTS ix_velaris_users_purge_after ON velaris_users (purge_after);
