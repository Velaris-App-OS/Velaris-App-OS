-- 070: system_config table for admin-managed settings (route permission matrix)

CREATE TABLE IF NOT EXISTS system_config (
    key         VARCHAR(255) PRIMARY KEY,
    value       JSONB        NOT NULL,
    updated_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_by  VARCHAR(255)
);

-- Default route permission map — mirrors current hardcoded NAV_ITEMS roles
INSERT INTO system_config (key, value) VALUES (
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
    }'::jsonb
) ON CONFLICT (key) DO NOTHING;
