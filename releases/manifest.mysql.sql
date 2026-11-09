-- Velaris Release Manifest — MySQL variant (DB SDK Phase 1).
--
-- MySQL-dialect sibling of releases/manifest.sql. start-velaris.sh applies THIS file
-- when database: mysql; the Postgres manifest.sql is used for Postgres. Keep the two
-- in lockstep: when you append a feature INSERT to manifest.sql, add the same row here.
--
-- Dialect differences vs manifest.sql:
--   gen_random_uuid()                  → UUID()
--   ON CONFLICT (feature_key) DO UPDATE → ON DUPLICATE KEY UPDATE <same columns>
--     (a precise no-op on the feature_key UNIQUE key — same "keep existing row" semantics,
--      and unlike INSERT IGNORE it does not also swallow unrelated errors).
--
-- DO NOT replace VALUES(col) with the row-alias form (`... VALUES (...) AS n ... n.col`).
-- MySQL 8.4 raises warning 1287 saying VALUES() is deprecated and suggesting the alias,
-- but start-velaris.sh routes BOTH mysql and mariadb here (DB_FAMILY=mysql), and
-- MariaDB 11.8 rejects the alias form outright with ERROR 1064. VALUES() is the only
-- spelling both engines accept today; taking the 8.4 warning at face value would break
-- every MariaDB deploy. If MySQL ever removes VALUES(), this file has to split by engine.
-- Execution-verified 2026-08-15 on MySQL 8.4.11 and MariaDB 11.8.8.
--
-- Rules (same as manifest.sql):
--   - One INSERT per feature. Never delete existing INSERTs.
--   - feature_key must match the is_feature_enabled() flag name in code.
--   - Schema changes go in migrations/, never here.

-- ── Future releases: append below this line ───────────────────────────────────
-- Template:
--
-- INSERT INTO scheduled_releases
--   (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
-- VALUES
--   (UUID(), 'feature_key', 'v1.0.0',
--    'Feature Title', 'One-line description',
--    'released', 'v1.0.0', NOW(), NOW(), NOW())
-- ON DUPLICATE KEY UPDATE
--   version     = VALUES(version),
--   title       = VALUES(title),
--   description = VALUES(description),
--   enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
--   updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.0.0 — feature flags shipped with the Velaris 2.0.0 platform release.
-- ══════════════════════════════════════════════════════════════════════════════

-- Marketplace
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'marketplace', 'v1.3.0',
   'Marketplace', 'Velaris App Store — browse, sandbox, and install platform extensions (official + community tiers)',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- Customer Accounts
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'customer_accounts', 'v1.0.2',
   'Customer Accounts', 'Persistent portal identity — OTP login, case history, GDPR erasure',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- HxDB Manager
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxdbmanager', 'v1.0.0',
   'HxDB Manager', 'AI-powered database operations panel inside Studio',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- HxDB Manager Security
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxdbmanager_security', 'v1.0.1',
   'HxDB Manager Security', 'Credential masking, breach detection, account auto-disable, DML before-image log',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.2.0 — flags shipped after v2.0.0. HxSentinel completed 2026-07-22; voice
-- input and voice form-fill 2026-07-25 → 2026-07-28. Versions match releases.txt.
-- Voice degrades honestly without the `intelligence` extra (faster-whisper):
-- the status endpoint reports no engine and the mic renders nothing at all.
-- ══════════════════════════════════════════════════════════════════════════════

-- HxSentinel
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxsentinel', 'v1.9.0',
   'HxSentinel', 'Risk engine — ground-truth capture, rules + verify gate, similarity oversight, signal packs, local scoring model',
   'released', 'v1.9.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- Voice Input
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'voice_input', 'v1.1.0',
   'Voice Input', 'Sovereign speech-to-text — local Whisper dictation in Studio ask boxes, form fields, and the customer portal',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- Voice Form-Fill
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'voice_fill', 'v1.1.0',
   'Voice Form-Fill', 'Spoken answers proposed as form values, every candidate quoting the transcript verbatim; never auto-committed',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
-- Enterprise identity (SAML 2.0 SP + SCIM 2.0 + the provisioning spine),
-- 2026-08-16 — NO ENTRY, deliberately. Mirrors the PostgreSQL manifest.
--
-- This file governs `is_feature_enabled()` flags and enterprise identity uses
-- none: SAML is live when a complete, enabled `sso_providers` row with
-- `provider='saml'` exists, SCIM when an unrevoked `scim_tokens` row does, and
-- the provisioning spine is a library the existing `bulk-sync` endpoint already
-- calls. A flag on top would be a second switch that can disagree with the
-- first.
-- ══════════════════════════════════════════════════════════════════════════════

-- ══════════════════════════════════════════════════════════════════════════════
-- Omnichannel inbound and the RPA bot runner, 2026-08-16 — NO ENTRIES.
-- Mirrors the PostgreSQL manifest.
--
-- Omnichannel is on when a `channel_endpoints` row with a signing secret
-- exists; the bot runner is the ENV switch VELARIS_CASE_BOT_RUNNER_ENABLED
-- (default off, because the loop starts containers on the host). A flag on top
-- of either would be a second switch that can disagree with the first.
-- ══════════════════════════════════════════════════════════════════════════════

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.4.0, 2026-08-19 — enterprise identity, omnichannel inbound, RPA bots,
-- the finished authorization rebuild, and the mobile export.
--
-- ONE version bump, and NO new feature keys. Every capability in this release
-- is switched on by its own configuration, and this file's rule is one switch
-- per capability:
--
--   * **SAML / SCIM** — on when an `sso_providers` row exists and is enabled;
--     with no row there is no button and every SCIM route answers 401.
--   * **omnichannel** — on when a `channel_endpoints` row exists with a signing
--     secret. Without one there is no webhook to call and no number to text.
--   * **bot_runner** — the ENV switch `VELARIS_CASE_BOT_RUNNER_ENABLED`,
--     default off, because the loop starts containers on the host and that is a
--     per-deployment operator choice. Env switches do not appear in this file.
--   * **App Builder** and the **authorization** work are core platform, not
--     gated capabilities.
--
-- A row on top of any of those would be a second switch that can disagree with
-- the first, and the failure mode is an operator who has configured the feature
-- correctly and cannot see why it does nothing.
-- ══════════════════════════════════════════════════════════════════════════════

-- ══════════════════════════════════════════════════════════════════════════════
-- The scheduler, 2026-08-20 — NO ENTRY, and the reasoning differs from the
-- others above.
--
-- `VELARIS_CASE_SCHEDULER_ENABLED` is an ENV switch, and env switches do not
-- appear in this file. It is **on by default**, unlike the bot runner, because
-- the loop writes rows and sends mail rather than starting containers on the
-- host — and because it is already gated by something stronger than a flag: a
-- schedule does not fire until somebody who is not its author approves it. On a
-- deployment where nobody has approved anything, an enabled loop does nothing at
-- all, so "on by default" costs a query every twenty seconds and no behaviour.
--
-- A feature row here would be a second switch that can disagree with the
-- approval gate, and the failure mode is the one this file keeps warning about:
-- an operator who has approved a schedule and cannot see why it never runs.
-- ══════════════════════════════════════════════════════════════════════════════

-- HxSentinel — v1.9.0 → v1.10.0.
-- Not a new capability: the gate shipped in v2.2.0 was near-inert. Four of its
-- five rules read the SUBJECT case's own signals, which an open case never had,
-- so the rules could not fire on the cases they exist to hold. Signals are now
-- extracted at evaluation time. The version advances so an operator can tell a
-- deployment that holds cases from one that only appeared to.
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxsentinel', 'v1.10.0',
   'HxSentinel', 'Risk engine — ground-truth capture, rules + verify gate, similarity oversight, signal packs, local scoring model',
   'released', 'v1.10.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- ════════════════════════════════════════════════════════════════════════════
-- v2.5.0, 2026-08-24 — the service split, proven boundaries, and enforcement
-- of things that were already described.
--
-- ONE version bump, and NO new feature keys. Again, and for the same reason:
-- every capability in this release is switched on by its own configuration, and
-- this file's rule is one switch per capability.
--
--   * **service extraction / gateway routing** — architecture, not a feature.
--     Nothing to enable; the prefixes clients call did not move.
--   * **workload identity** — on when VELARIS_GATEWAY_IDENTITY_SECRET is set.
--     Inert when it is not, which is what lets an upgrade stay silent.
--   * **internal mTLS** — a mount swap plus VELARIS_UPSTREAM_SCHEME. Off by
--     default because on a single host there is no wire to listen on.
--   * **HxShield detection** — the ENV switch
--     VELARIS_CASE_HXSHIELD_DETECT_ENABLED, default off. Env switches do not
--     appear in this file.
--   * **People Calendar** and **HxDeploy promotion** are core module surfaces
--     reached through their own routes and gated by hxguard actions, not by a
--     release row.
--   * **manifest permission enforcement** — a property of every install from
--     migration 174 onward, not something an operator turns on. A row would
--     imply it can be turned off, and it cannot.
--
-- A row on top of any of those would be a second switch that can disagree with
-- the first, and the failure mode is the one this file keeps warning about: an
-- operator who has configured the feature correctly and cannot see why it does
-- nothing.

-- ──────────────────────────────────────────────────────────────────────────────
-- v2.5.1, 2026-08-27 — calendar import, subscriptions and export.
--
-- **No rows here either.** A subscription is created by a user and reaches only
-- the hosts named in VELARIS_CASE_CALENDAR_FEED_HOSTS; unset, the feature cannot
-- be used at all. That environment variable IS the switch, and a manifest row on
-- top of it would be a second one that can disagree with the first.
--
-- Migrations 175 and 176 are additive: two columns on calendar_events, one on
-- user_absences, one new table, and four indexes. Nothing renamed, nothing
-- dropped, and every existing row keeps the behaviour it had — source defaults
-- to 'manual', which is what every calendar entry created before this was.
-- ══════════════════════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────────────────────
-- v3.0.0, prepared 2026-09-17 — HxArbiter, the AI-era features F-00…F-12, the
-- rep desk, case actions and statuses, and the market-gap backlog.
--
-- **No rows.** Every feature in this release has exactly one switch already, and
-- a manifest row would be a second one that can disagree with it:
--   * HxArbiter — its own namespace ceilings, every one shipped at `shadow`;
--   * F-00…F-12, audit streaming, OCR, HxShield responses — their own settings,
--     all unset = off;
--   * the rep desk — the platform release flag AND the tenant's marketplace
--     install, both absent on upgrade;
--   * bulk stage moves and moving a case between tenants — new privileges held by
--     administrators, no switch at all.
--
-- The one breaking change is not a feature key: migration 198 removes six case
-- routes (see releases/notes/v3.0.0.md §Case actions). Migrations 177–205 are
-- additive apart from that route change and 197's data-only tenant backfill.
-- ══════════════════════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────────────────────
-- v3.1.0, prepared 2026-10-11 — legal holds, staged content rollouts, the rest
-- of the rep desk, tenant-owned design content, and the tenant-boundary sweep.
--
-- **No rows.** Nothing in this release has a feature key:
--   * legal holds, content rollouts, the customer export — privileges held by
--     named roles; nothing is held or staged until somebody does it;
--   * chunking / rerank, the three privacy switches, field-node signatures —
--     their own settings, all unset = off;
--   * the rep desk additions sit behind the rep desk's existing two gates.
--
-- Migrations 206–217 are additive (see releases/notes/v3.1.0.md §Upgrade notes).
-- ══════════════════════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────────────────────
-- v3.1.2, prepared 2026-10-12 — the update path and document storage.
--
-- **No rows, no migrations.** The updater survives a failed image pull, reports
-- a stopped update and finishes one when re-run; a new install stores documents
-- on local disk and the bundled MinIO is kept only where it already runs.
-- See releases/notes/v3.1.2.md.
-- ══════════════════════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────────────────────
-- v3.2.0, prepared 2026-11-08 — the service split, the tenant boundary, and
-- backup / restore / recovery.
--
-- **No rows.** Nothing in this release has a feature key:
--   * the new services, pooler, cache, Kafka and OpenBao raft are deployment,
--     not features; the event bus stays off unless turned on;
--   * Marketplace + Customer Accounts on a NEW PostgreSQL install are released
--     by setup-velaris.sh (Step 8c), never by this manifest;
--   * Form Builder, connector operations and the database query connector sit
--     behind their existing privileges.
--
-- Migrations 218–231 are additive (see releases/notes/v3.2.0.md §Upgrade notes).
-- ══════════════════════════════════════════════════════════════════════════════
