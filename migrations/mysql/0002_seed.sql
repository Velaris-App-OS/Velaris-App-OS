-- ============================================================================
-- Velaris — MySQL bootstrap seed (DB SDK Phase 1).
--
-- The 0001 baseline (ORM-metadata-generated) carries the SCHEMA, including
-- helix_settings + system_config (now modeled). This file supplies only the
-- boot-critical SEED rows that the metadata baseline cannot carry:
--   default tenant (007), portals/roles/admin group (033),
--   route-permission map (070), auth settings (068).
--
-- Dialect port vs the PG migrations: gen_random_uuid()→UUID(); ON CONFLICT … DO
-- NOTHING→ON DUPLICATE KEY UPDATE <key>=<key>; ::jsonb cast dropped;
-- and EVERY NOT NULL column is supplied explicitly because the PG server-side
-- DEFAULTs (id/timestamps/flags) do not exist on the metadata-built MySQL schema.
--
-- Idempotent: re-applying is a no-op (unique/primary keys collide → UPDATE self).
-- Deferrable seed (demo users, bpm concepts, retention, calendars, …) is NOT here.
-- ============================================================================

-- ── default tenant (007) ──────────────────────────────────────────────────────
INSERT INTO tenants (id, slug, name, description, status, settings, created_at, updated_at)
VALUES (UUID(), 'default', 'Default Tenant', 'Default tenant for pre-Phase 17 data',
        'active', '{}', NOW(), NOW())
ON DUPLICATE KEY UPDATE slug = slug;

-- ── system portals (033) ──────────────────────────────────────────────────────
INSERT INTO portals (id, name, portal_type, modules, homepage, theme, is_active, created_at, updated_at) VALUES
('00000000-0000-0000-0000-000000000001', 'Staff Studio', 'staff',
 '["dashboard","work-center","case-manager","analytics","process-mining","hxnexus","portal-admin","email-admin","push-admin","admin","tenants","scout"]',
 '/work-center', '{}', TRUE, NOW(), NOW()),
('00000000-0000-0000-0000-000000000002', 'Manager Portal', 'manager',
 '["dashboard","analytics","process-mining","work-center","hxnexus","admin"]',
 '/analytics', '{}', TRUE, NOW(), NOW()),
('00000000-0000-0000-0000-000000000003', 'Admin Console', 'admin',
 '["admin","tenants","user-directory","access-groups","portal-admin","email-admin","push-admin","analytics","scout","hxnexus"]',
 '/admin', '{}', TRUE, NOW(), NOW())
ON DUPLICATE KEY UPDATE id = id;

-- ── system access roles (033) — names match require_role() strings ────────────
INSERT INTO access_roles (id, name, description, privileges, created_at, updated_at) VALUES
('00000000-0000-0001-0000-000000000001', 'admin', 'Full system access — all resources, all actions',
 '[{"resource":"*","case_type_id":"*","actions":["*"]}]', NOW(), NOW()),
('00000000-0000-0001-0000-000000000002', 'staff', 'Standard case worker — create, read and work cases',
 '[{"resource":"case","case_type_id":"*","actions":["create","read","update","assign"]},{"resource":"case_type","actions":["read"]},{"resource":"document","actions":["read","upload"]}]',
 NOW(), NOW()),
('00000000-0000-0001-0000-000000000003', 'viewer', 'Read-only access across all case types',
 '[{"resource":"case","case_type_id":"*","actions":["read"]},{"resource":"case_type","actions":["read"]}]',
 NOW(), NOW())
ON DUPLICATE KEY UPDATE id = id;

-- ── default Administrators group (033) ────────────────────────────────────────
INSERT INTO access_groups
  (id, name, description, tenant_id, portal_id, role_ids,
   allowed_case_type_ids, allowed_queue_ids, is_default, is_active, created_at, updated_at)
VALUES
  ('00000000-0000-0002-0000-000000000001', 'Administrators',
   'Default admin group — full access to all modules', 'system',
   '00000000-0000-0000-0000-000000000001',
   '["00000000-0000-0001-0000-000000000001"]', '["*"]', '["*"]',
   TRUE, TRUE, NOW(), NOW())
ON DUPLICATE KEY UPDATE id = id;

-- ── route permission map (070) ────────────────────────────────────────────────
INSERT INTO system_config (`key`, `value`, updated_at) VALUES (
  'route_permissions',
  '{
    "/sitemap":        ["admin","manager","designer","developer"],
    "/analytics":      ["case_worker","admin"],
    "/hxanalytics":    ["case_worker","admin"],
    "/documents":      ["case_worker","admin"],
    "/inbox":          ["case_worker","admin"],
    "/case-designer":  ["designer","admin"],
    "/form-builder":   ["designer","admin"],
    "/nlp-builder":    ["designer","admin"],
    "/modeler":        ["designer","admin"],
    "/app-builder":    ["designer","admin"],
    "/hxwork":         ["designer","admin"],
    "/importer":       ["designer","admin"],
    "/graph":          ["designer","admin"],
    "/process-mining": ["designer","admin"],
    "/live-activity":  ["designer","admin"],
    "/monitor":        ["designer","admin"],
    "/deploy":         ["devops","admin"],
    "/app-registry":   ["devops","admin"],
    "/hxmigrate":      ["devops","admin"],
    "/scout":          ["devops","admin"],
    "/scout-ai":       ["devops","admin"],
    "/orchestrator":   ["devops","admin"],
    "/hxconnect":      ["integration","admin"],
    "/hxbridge":       ["integration","admin"],
    "/devconn":        ["integration","admin","designer"],
    "/hxsync":         ["integration","admin"],
    "/hxfusion":       ["integration","admin","designer"],
    "/hxshield":       ["security","admin"],
    "/hxstream":       ["security","admin","designer"],
    "/hxlogs":         ["security","admin","designer"],
    "/compliance":     ["security","admin"],
    "/observability":  ["security","admin"],
    "/portal-admin":   ["admin"],
    "/access-groups":  ["admin"],
    "/user-directory": ["admin"],
    "/admin":          ["admin"],
    "/tenants":        ["admin"],
    "/enterprise":     ["admin"],
    "/email-admin":    ["admin"],
    "/push-admin":     ["admin"],
    "/hxglobal":       ["admin"],
    "/escalation":     ["admin","designer"]
  }',
  NOW()
) ON DUPLICATE KEY UPDATE `key` = `key`;

-- ── auth settings (068) ───────────────────────────────────────────────────────
INSERT INTO helix_settings (`key`, `value`, updated_at) VALUES ('token_expiry_days', '60', NOW())
ON DUPLICATE KEY UPDATE `key` = `key`;

-- ── case-variable namespaces (087) ────────────────────────────────────────────
-- CORRECTNESS-critical (not just parity): the case-variables-v2 resolver
-- (case_vars/service.py) looks these up BY NAME; without the 'form'/'portal' rows a
-- form/portal variable write raises "No registered namespace … register the
-- integration first." created_at is supplied explicitly (no server default on the
-- metadata-built MySQL schema — the error-1364 class).
INSERT INTO variable_namespaces
  (id, name, owner_type, sensitivity, status, created_by, created_at) VALUES
  (UUID(), 'velaris', 'platform', 'internal', 'active', 'migration-087', NOW()),
  (UUID(), 'form',    'form',     'pii',      'active', 'migration-087', NOW()),
  (UUID(), 'portal',  'portal',   'pii',      'active', 'migration-087', NOW()),
  (UUID(), 'legacy',  'platform', 'internal', 'active', 'migration-087', NOW())
ON DUPLICATE KEY UPDATE name = name;

-- ── deferrable parity seed (PG migrations 002/009/084) ────────────────────────
-- These are PARITY (not correctness): each consumer falls back gracefully when the
-- row is absent (SLA → 24/7, retention → disabled, PUO → in-code defaults). Ported so
-- a fresh MySQL install matches a fresh PG install. id/created_at/updated_at supplied
-- explicitly (no server defaults on the metadata-built MySQL schema). JSON columns use
-- valid JSON ('[1,2,3,4,5]', not PG's '{1,2,3,4,5}' array literal).

-- default business calendar (002) — Mon–Fri 9–17 UTC
INSERT INTO business_calendars
  (id, name, timezone, work_days, work_start_hour, work_end_hour, holidays, description, created_at, updated_at) VALUES
  (UUID(), 'default', 'UTC', '[1,2,3,4,5]', 9, 17, '[]',
   'Default business calendar: Mon-Fri 9-17 UTC', NOW(), NOW())
ON DUPLICATE KEY UPDATE name = name;

-- default retention policies (009) — all disabled by default
INSERT INTO retention_policies
  (id, name, resource_type, retention_days, action, enabled, created_at) VALUES
  (UUID(), 'Resolved case retention',   'resolved_cases',  2555, 'archive', FALSE, NOW()),
  (UUID(), 'Audit log retention',       'audit_log',       2555, 'archive', FALSE, NOW()),
  (UUID(), 'Security event retention',  'security_events', 1095, 'archive', FALSE, NOW()),
  (UUID(), 'Process mining event log',  'event_log',        365, 'delete',  FALSE, NOW())
ON DUPLICATE KEY UPDATE resource_type = resource_type;

-- platform update settings singleton (084) — defaults supplied (PG relies on server defaults)
INSERT INTO platform_update_settings (id, mode, default_soak_hours, updated_at)
VALUES (1, 'auto-soak', 48, NOW())
ON DUPLICATE KEY UPDATE id = id;
