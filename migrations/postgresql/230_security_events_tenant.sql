-- 230 — security_events.tenant_id (2026-11-04, the per-tenant audit stream).
--
-- WHY. A security event carried no tenant, so the audit stream could only be
-- the platform operator's — one stream of every tenant's events. Each event now
-- records the TRUSTED tenant of the signed-in request that raised it (sign-in
-- sets it; never a header), so a tenant's admin can stream and read its own.
--
-- WHAT DOES NOT CHANGE. Additive: one nullable column and an index. NULL is the
-- platform's (every event before this, and events raised outside a request),
-- which the platform operator alone reads — exactly today's behaviour. The
-- previous release's code neither reads nor writes the column.
--
-- LIVE-SAFE. ADD COLUMN with no default is a catalogue change, not a rewrite;
-- the index is built CONCURRENTLY (the runners feed this file to psql without
-- a wrapping transaction).
--
-- RE-RUNNABLE. IF NOT EXISTS on the column; an index build that stopped part
-- way is dropped and built again.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE security_events ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(255);
DROP INDEX CONCURRENTLY IF EXISTS idx_security_events_tenant;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_security_events_tenant ON security_events (tenant_id, timestamp);
