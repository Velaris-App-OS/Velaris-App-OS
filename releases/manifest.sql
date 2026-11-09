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
--   - ON CONFLICT DO UPDATE: the file is an idempotent upsert, so re-applying it
--     refreshes version/title/description on rows that already exist. It does
--     NOT re-enable a feature an operator disabled, and never rewrites
--     status/released_at. (It used to be DO NOTHING, which meant a version bump
--     here could never reach an installed instance.)
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
-- ON CONFLICT (feature_key) DO UPDATE SET
--   version     = EXCLUDED.version,
--   title       = EXCLUDED.title,
--   description = EXCLUDED.description,
--   enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
--                      THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
--   updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.0.0 — feature flags shipped with the Velaris 2.0.0 platform release.
-- Versions match releases.txt. Only is_feature_enabled() keys appear here;
-- the platform itself ("velaris") and marketplace apps ("hxtest", install-gated
-- via marketplace_installs) are NOT feature flags and are documented in
-- releases/notes/v2.0.0.md, not inserted below.
-- ══════════════════════════════════════════════════════════════════════════════

-- Marketplace
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'marketplace', 'v1.3.0',
   'Marketplace', 'Velaris App Store — browse, sandbox, and install platform extensions (official + community tiers)',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO UPDATE SET
  version     = EXCLUDED.version,
  title       = EXCLUDED.title,
  description = EXCLUDED.description,
  -- Advance `enabled` ONLY when the feature is already on. Re-running the
  -- manifest must refresh the shipped version, never re-enable something an
  -- operator deliberately turned off. `status` and `released_at` are never
  -- touched: they record the original release moment.
  enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
                     THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
  updated_at  = NOW();

-- Customer Accounts
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'customer_accounts', 'v1.0.2',
   'Customer Accounts', 'Persistent portal identity — OTP login, case history, GDPR erasure',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO UPDATE SET
  version     = EXCLUDED.version,
  title       = EXCLUDED.title,
  description = EXCLUDED.description,
  -- Advance `enabled` ONLY when the feature is already on. Re-running the
  -- manifest must refresh the shipped version, never re-enable something an
  -- operator deliberately turned off. `status` and `released_at` are never
  -- touched: they record the original release moment.
  enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
                     THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
  updated_at  = NOW();

-- HxDB Manager
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'hxdbmanager', 'v1.0.0',
   'HxDB Manager', 'AI-powered database operations panel inside Studio',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO UPDATE SET
  version     = EXCLUDED.version,
  title       = EXCLUDED.title,
  description = EXCLUDED.description,
  -- Advance `enabled` ONLY when the feature is already on. Re-running the
  -- manifest must refresh the shipped version, never re-enable something an
  -- operator deliberately turned off. `status` and `released_at` are never
  -- touched: they record the original release moment.
  enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
                     THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
  updated_at  = NOW();

-- HxDB Manager Security
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'hxdbmanager_security', 'v1.0.1',
   'HxDB Manager Security', 'Credential masking, breach detection, account auto-disable, DML before-image log',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO UPDATE SET
  version     = EXCLUDED.version,
  title       = EXCLUDED.title,
  description = EXCLUDED.description,
  -- Advance `enabled` ONLY when the feature is already on. Re-running the
  -- manifest must refresh the shipped version, never re-enable something an
  -- operator deliberately turned off. `status` and `released_at` are never
  -- touched: they record the original release moment.
  enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
                     THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
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
  (gen_random_uuid(), 'hxsentinel', 'v1.9.0',
   'HxSentinel', 'Risk engine — ground-truth capture, rules + verify gate, similarity oversight, signal packs, local scoring model',
   'released', 'v1.9.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO UPDATE SET
  version     = EXCLUDED.version,
  title       = EXCLUDED.title,
  description = EXCLUDED.description,
  -- Advance `enabled` ONLY when the feature is already on. Re-running the
  -- manifest must refresh the shipped version, never re-enable something an
  -- operator deliberately turned off. `status` and `released_at` are never
  -- touched: they record the original release moment.
  enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
                     THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
  updated_at  = NOW();

-- Voice Input
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'voice_input', 'v1.1.0',
   'Voice Input', 'Sovereign speech-to-text — local Whisper dictation in Studio ask boxes, form fields, and the customer portal',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO UPDATE SET
  version     = EXCLUDED.version,
  title       = EXCLUDED.title,
  description = EXCLUDED.description,
  -- Advance `enabled` ONLY when the feature is already on. Re-running the
  -- manifest must refresh the shipped version, never re-enable something an
  -- operator deliberately turned off. `status` and `released_at` are never
  -- touched: they record the original release moment.
  enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
                     THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
  updated_at  = NOW();

-- Voice Form-Fill
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'voice_fill', 'v1.1.0',
   'Voice Form-Fill', 'Spoken answers proposed as form values, every candidate quoting the transcript verbatim; never auto-committed',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO UPDATE SET
  version     = EXCLUDED.version,
  title       = EXCLUDED.title,
  description = EXCLUDED.description,
  -- Advance `enabled` ONLY when the feature is already on. Re-running the
  -- manifest must refresh the shipped version, never re-enable something an
  -- operator deliberately turned off. `status` and `released_at` are never
  -- touched: they record the original release moment.
  enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
                     THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
  updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.3.0 — NO NEW ENTRIES, deliberately.
--
-- Recorded explicitly because this file was found three flags behind at v2.2.0,
-- and "did someone forget again?" is otherwise unanswerable without auditing
-- the whole release.
--
-- v2.3.0 shipped HxConsent, HxPrivacy, HxLedger, HxDR, HxStatus, event
-- sourcing, the CDC event bus, CRDT convergence, the Temporal interpreter,
-- LLMOps and `ai_action` — and **none of them is an is_feature_enabled() flag**,
-- which is the only thing this file governs. They are either always-on routes
-- gated by role, or switched by environment variable:
--
--   VELARIS_CASE_LEDGER_POSTING_ENABLED   (default ON)
--   VELARIS_CASE_HEALTH_SAMPLER_ENABLED   (default ON)
--   VELARIS_CASE_EVENT_SOURCING_ENABLED   (default OFF)
--   VELARIS_CASE_EVENT_BUS_ENABLED        (default OFF)
--   VELARIS_CASE_TEMPORAL_INTERPRETER_ENABLED (default OFF)
--   VELARIS_CASE_HXSANDBOX_ENABLED        (default OFF)
--
-- Env switches are per-deployment operator choices; a database row would make
-- them look centrally released, which they are not. Verify with:
--   grep -rho 'is_feature_enabled("[a-z_]*"' services/case-service | sort -u
-- ══════════════════════════════════════════════════════════════════════════════

-- ══════════════════════════════════════════════════════════════════════════════
-- Enterprise identity (SAML 2.0 SP + SCIM 2.0 + the provisioning spine),
-- 2026-08-16 — NO ENTRY, deliberately.
--
-- Recorded here for the same reason as the block above: "did someone forget
-- again?" should be answerable without auditing the release.
--
-- This file governs `is_feature_enabled()` flags, and enterprise identity uses
-- none. It is switched on by *configuration existing*, which is stronger than a
-- flag and cannot drift out of step with it:
--
--   * SAML is live for a tenant when an `sso_providers` row with
--     `provider='saml'` is enabled AND complete — `SamlConfig.missing()` refuses
--     a half-configured row on the request path, so a flag could not have
--     enabled it anyway;
--   * SCIM is live when a `scim_tokens` row exists and is unrevoked. No token,
--     no caller, and every SCIM route answers 401;
--   * the provisioning spine is a library the existing `bulk-sync` endpoint
--     already calls, so there is nothing to release.
--
-- A flag on top of that would be a second switch that can disagree with the
-- first — and the failure mode is an administrator who has configured SAML
-- correctly and cannot see why it does nothing.
-- ══════════════════════════════════════════════════════════════════════════════

-- ══════════════════════════════════════════════════════════════════════════════
-- Omnichannel inbound and the RPA bot runner, 2026-08-16 — NO ENTRIES.
--
-- Both design documents asked for a flag here (`omnichannel`, `bot_runner`).
-- Neither got one, for the reason this file's v2.3.0 block already states: a
-- database row makes a switch look centrally released, and these are not.
--
--   * **omnichannel** is on when a `channel_endpoints` row exists with a signing
--     secret. Without one there is no webhook to call and no number to text, so
--     a flag would gate nothing; with one, a flag set to off would leave an
--     admin staring at a configured endpoint that silently drops messages.
--   * **bot_runner** is an ENV switch, `VELARIS_CASE_BOT_RUNNER_ENABLED`,
--     default off — as the design itself specified, because the loop starts
--     containers on the host and that is a per-deployment operator choice, not
--     a release. Env switches deliberately do not appear in this file.
--
-- The rule being followed: one switch per capability. A flag on top of either of
-- these would be a second switch that can disagree with the first, and the
-- failure mode is an operator who has configured the feature correctly and
-- cannot see why it does nothing.
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
  (gen_random_uuid(), 'hxsentinel', 'v1.10.0',
   'HxSentinel', 'Risk engine — ground-truth capture, rules + verify gate, similarity oversight, signal packs, local scoring model',
   'released', 'v1.10.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO UPDATE SET
  version     = EXCLUDED.version,
  title       = EXCLUDED.title,
  description = EXCLUDED.description,
  -- Advance `enabled` ONLY when the feature is already on. Re-running the
  -- manifest must refresh the shipped version, never re-enable something an
  -- operator deliberately turned off. `status` and `released_at` are never
  -- touched: they record the original release moment.
  enabled     = CASE WHEN scheduled_releases.enabled IS NOT NULL
                     THEN EXCLUDED.version ELSE scheduled_releases.enabled END,
  updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
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
