-- R5e step 5 — change counters for caches that must agree between processes.
-- See migrations/postgresql/219_platform_generations.sql for the reasoning.
-- Additive and re-runnable.
CREATE TABLE IF NOT EXISTS platform_generations (
    topic       VARCHAR(40)  PRIMARY KEY,
    generation  BIGINT       NOT NULL DEFAULT 0,
    updated_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    reason      VARCHAR(100)
);

INSERT INTO platform_generations (topic, generation, reason) VALUES
    ('flags', 0, 'seed'), ('revocation', 0, 'seed'),
    ('vault', 0, 'seed'), ('tenants', 0, 'seed')
ON DUPLICATE KEY UPDATE topic = topic;
