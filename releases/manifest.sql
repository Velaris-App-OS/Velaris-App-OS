-- Velaris Release Manifest
--
-- This file is the single source of truth for which features are active
-- on a Velaris instance. start-velaris.sh compares the INSERT count here
-- against the row count in scheduled_releases — if they differ, this file
-- is applied automatically.
--
-- Rules:
--   - One INSERT per feature. Never delete existing INSERTs.
--   - To release a new feature: append a new INSERT block at the bottom.
--   - ON CONFLICT DO NOTHING: existing rows are preserved, only new ones inserted.
--   - Do not use this file for schema changes — use migrations/ for that.
--
-- Format:
--   feature_key  must match the flag name used in is_feature_enabled() in code
--   version      matches the entry in releases.txt on the website server
--   enabled      same as version — activates the feature on install

-- ── v1.0.0 — no hidden feature flags; all core features ship by default ───────
-- (append future release INSERT blocks below when ready to ship)

-- ── Future releases: append below this line ───────────────────────────────────
-- Template:
--
-- INSERT INTO scheduled_releases
--   (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
-- VALUES
--   (gen_random_uuid(), 'feature_key', 'v1.0.0',
--    'Feature Title', 'One-line description',
--    'released', 'v1.0.0', NOW(), NOW(), NOW())
-- ON CONFLICT (feature_key) DO NOTHING;

-- ── Pending future releases (uncomment when shipping) ────────────────────────

-- v1.2.0 — Marketplace
-- INSERT INTO scheduled_releases
--   (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
-- VALUES
--   (gen_random_uuid(), 'marketplace', 'v1.2.0',
--    'Marketplace', 'Velaris App Store — browse, sandbox, and install platform extensions',
--    'released', 'v1.2.0', NOW(), NOW(), NOW())
-- ON CONFLICT (feature_key) DO NOTHING;

-- v1.2.1 — Customer Accounts
-- INSERT INTO scheduled_releases
--   (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
-- VALUES
--   (gen_random_uuid(), 'customer_accounts', 'v1.2.1',
--    'Customer Accounts', 'Persistent portal identity — OTP login, case history, GDPR erasure',
--    'released', 'v1.2.1', NOW(), NOW(), NOW())
-- ON CONFLICT (feature_key) DO NOTHING;

-- v1.3.0 — HxDB Manager
-- INSERT INTO scheduled_releases
--   (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
-- VALUES
--   (gen_random_uuid(), 'hxdbmanager', 'v1.3.0',
--    'HxDB Manager', 'AI-powered database operations panel inside Studio',
--    'released', 'v1.3.0', NOW(), NOW(), NOW())
-- ON CONFLICT (feature_key) DO NOTHING;

-- v1.3.1 — HxDB Manager Security
-- INSERT INTO scheduled_releases
--   (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
-- VALUES
--   (gen_random_uuid(), 'hxdbmanager_security', 'v1.3.1',
--    'HxDB Manager Security', 'Credential masking, breach detection, account auto-disable, DML before-image log',
--    'released', 'v1.3.1', NOW(), NOW(), NOW())
-- ON CONFLICT (feature_key) DO NOTHING;
