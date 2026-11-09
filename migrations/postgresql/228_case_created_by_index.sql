-- 228 — index case_instances.created_by (R6 soak, 2026-11-02).
--
-- WHY. The case list (`GET /cases`, repository.search_case_instances) shows a
-- user the cases they created, plus those a tuple or access-group type rule
-- grants. "Created by" had no index, so the list read every case in the
-- tenant: at 28,000 cases it took ~90 ms twice per call and, at 400 users,
-- PostgreSQL took the whole box (p95 17 s). With this index and the query
-- reshaped as `id IN (…)`, each branch is an index lookup.
--
-- WHAT DOES NOT CHANGE. Additive: one index, no data, no column. The previous
-- release's code is unaffected (it simply does not use it).
--
-- LIVE-SAFE. CONCURRENTLY: writes to case_instances are not blocked while it
-- builds. Every runner feeds this file to psql without a wrapping transaction
-- (CONCURRENTLY refuses to run inside one).
--
-- RE-RUNNABLE. A build that stopped part way leaves an INVALID index that
-- `IF NOT EXISTS` would then skip for good, so it is dropped first and built
-- again; on a clean run the drop finds nothing.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

DROP INDEX CONCURRENTLY IF EXISTS idx_cases_created_by;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cases_created_by ON case_instances (created_by);
