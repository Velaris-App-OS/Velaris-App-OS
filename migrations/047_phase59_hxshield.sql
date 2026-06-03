-- P59: HxShield — Case Fraud & Abuse Detection
-- security_rules:     configurable detection patterns with thresholds
-- security_incidents: first-class incident records (one per detection)
-- shield_events:      raw scored event log from the detection engine (renamed to avoid clash with Phase 20 security_events)

CREATE TABLE security_rules (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    pattern_type    VARCHAR(50)  NOT NULL,
    description     TEXT,
    threshold       INTEGER      NOT NULL DEFAULT 10,
    window_seconds  INTEGER      NOT NULL DEFAULT 600,
    action          VARCHAR(20)  NOT NULL DEFAULT 'flag',
    severity        VARCHAR(10)  NOT NULL DEFAULT 'medium',
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    tenant_id       VARCHAR(255),
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_shield_rules_pattern ON security_rules (pattern_type);
CREATE INDEX ix_shield_rules_enabled ON security_rules (enabled);

CREATE TABLE security_incidents (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         UUID         REFERENCES security_rules(id) ON DELETE SET NULL,
    pattern_type    VARCHAR(50)  NOT NULL,
    severity        VARCHAR(10)  NOT NULL DEFAULT 'medium',
    status          VARCHAR(20)  NOT NULL DEFAULT 'open',
    actor_id        VARCHAR(255),
    tenant_id       VARCHAR(255),
    case_type_id    VARCHAR(255),
    context         JSONB        NOT NULL DEFAULT '{}',
    explanation     TEXT,
    detected_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     VARCHAR(255)
);

CREATE INDEX ix_shield_inc_status   ON security_incidents (status);
CREATE INDEX ix_shield_inc_severity ON security_incidents (severity);
CREATE INDEX ix_shield_inc_actor    ON security_incidents (actor_id);
CREATE INDEX ix_shield_inc_tenant   ON security_incidents (tenant_id);
CREATE INDEX ix_shield_inc_detected ON security_incidents (detected_at DESC);

CREATE TABLE shield_events (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      VARCHAR(50)  NOT NULL,
    actor_id        VARCHAR(255),
    tenant_id       VARCHAR(255),
    case_type_id    VARCHAR(255),
    payload_hash    VARCHAR(64),
    score           FLOAT        NOT NULL DEFAULT 0.0,
    patterns_matched JSONB       NOT NULL DEFAULT '[]',
    raw_context     JSONB        NOT NULL DEFAULT '{}',
    recorded_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_shield_ev_actor    ON shield_events (actor_id);
CREATE INDEX ix_shield_ev_type     ON shield_events (event_type);
CREATE INDEX ix_shield_ev_recorded ON shield_events (recorded_at DESC);
