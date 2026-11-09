-- R5e step 5 (Nov_TODO) — change counters for caches that must agree between
-- processes, generalising the HxGuard row of migration 173.
--
-- Several in-process caches are correct only while ONE process serves the
-- platform: a feature published in one copy reached the others after their
-- hourly reload; a logout took up to 30 s to bite; a shredded tenant key stayed
-- usable in another copy for up to ~30 s; nothing cleared the tenant lookups on
-- a tenant change. One row per topic holds a counter. A change bumps its topic
-- in the same transaction as the change; every process reads all topics on a
-- short interval and reloads the caches whose counter moved. Works with the
-- cache (Valkey) switched off, needs no ordering, and a process that misses an
-- intermediate value still sees a different number.
--
-- Additive and re-runnable. The previous release's code never reads it.
CREATE TABLE IF NOT EXISTS platform_generations (
    topic       VARCHAR(40)  PRIMARY KEY,
    generation  BIGINT       NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    reason      VARCHAR(100)
);

-- Seeded so a reader always gets a number for every topic it knows.
INSERT INTO platform_generations (topic, generation, reason) VALUES
    ('flags', 0, 'seed'), ('revocation', 0, 'seed'),
    ('vault', 0, 'seed'), ('tenants', 0, 'seed')
ON CONFLICT (topic) DO NOTHING;
