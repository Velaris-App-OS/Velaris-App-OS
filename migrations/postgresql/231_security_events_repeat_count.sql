-- 231 — security_events.repeat_count (2026-11-05, R6 soak finding S2).
--
-- WHY. Every refused (401/403) mutating request wrote its own row: 60,724 rows
-- in 1.5 h of an unauthenticated flood, bounded only by the per-address rate
-- limit. Repeats of one refusal (same address, actor, route, status) within a
-- minute are now counted, and written as ONE summary row whose repeat_count
-- says how many events it stands for. Readers count SUM(repeat_count).
--
-- WHAT DOES NOT CHANGE. Every existing row is one event: DEFAULT 1. The
-- previous release's code neither reads nor names the column; its inserts get 1.
--
-- LIVE-SAFE. ADD COLUMN with a constant default is a catalogue change on
-- PostgreSQL 11+ (no table rewrite) but still takes an exclusive lock. Behind a
-- long query on security_events that lock would wait, and every audit write of
-- the running platform would queue behind it (the S9 shape, 2026-11-02). So it
-- is tried with a 0.5 s lock_timeout, up to 90 times, 0.5 s apart: a write is never held up more than 0.5 s.
-- If the table stays busy the migration stops with a clear message and the
-- update can simply be run again. An install that already has the column takes
-- no lock at all (catalogue check first).
--
-- RE-RUNNABLE. Catalogue check, then IF NOT EXISTS.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

DO $$
DECLARE
  attempt int;
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'security_events' AND column_name = 'repeat_count') THEN
    RETURN;
  END IF;
  FOR attempt IN 1..90 LOOP
    BEGIN
      PERFORM set_config('lock_timeout', '500ms', true);
      ALTER TABLE security_events ADD COLUMN IF NOT EXISTS repeat_count INTEGER NOT NULL DEFAULT 1;
      RETURN;
    EXCEPTION WHEN lock_not_available THEN
      PERFORM pg_sleep(0.5);
    END;
  END LOOP;
  RAISE EXCEPTION '231: security_events stayed busy for ~90 s; nothing was changed — run the update again';
END
$$;
