-- HELIX Phase 20 Migration: Enterprise Hardening
BEGIN;

-- Security events — structured audit for SOC2
CREATE TABLE IF NOT EXISTS security_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      VARCHAR(50) NOT NULL,   -- auth.login, data.access, data.export, data.delete, etc.
    severity        VARCHAR(20) DEFAULT 'info',  -- info, warning, critical
    user_id         VARCHAR(255),
    resource_type   VARCHAR(100),           -- case, case_type, tenant, etc.
    resource_id     VARCHAR(255),
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    action          VARCHAR(100),           -- view, create, update, delete, export
    outcome         VARCHAR(20),            -- success, denied, error
    details         JSONB DEFAULT '{}',
    timestamp       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_security_events_type ON security_events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_security_events_user ON security_events(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_security_events_resource ON security_events(resource_type, resource_id);

-- Data retention policies
CREATE TABLE IF NOT EXISTS retention_policies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    resource_type   VARCHAR(100) NOT NULL,  -- case, audit_log, security_event, event_log
    retention_days  INT NOT NULL,
    action          VARCHAR(20) DEFAULT 'archive',  -- archive, delete, anonymize
    enabled         BOOLEAN DEFAULT true,
    last_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(resource_type)
);

-- GDPR data subject requests
CREATE TABLE IF NOT EXISTS gdpr_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id      VARCHAR(255) NOT NULL,   -- the user_id whose data is being requested
    request_type    VARCHAR(50) NOT NULL,    -- export, delete, rectify, restrict
    status          VARCHAR(30) DEFAULT 'pending',  -- pending, processing, completed, rejected
    requested_by    VARCHAR(255),
    reason          TEXT,
    result_file     TEXT,                     -- path/reference to export file
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gdpr_subject ON gdpr_requests(subject_id);
CREATE INDEX IF NOT EXISTS idx_gdpr_status ON gdpr_requests(status);

-- Seed default retention policies
INSERT INTO retention_policies (name, resource_type, retention_days, action, enabled)
VALUES
    ('Resolved case retention', 'resolved_cases', 2555, 'archive', false),   -- 7 years
    ('Audit log retention', 'audit_log', 2555, 'archive', false),            -- 7 years
    ('Security event retention', 'security_events', 1095, 'archive', false), -- 3 years
    ('Process mining event log', 'event_log', 365, 'delete', false)          -- 1 year
ON CONFLICT (resource_type) DO NOTHING;

COMMIT;
