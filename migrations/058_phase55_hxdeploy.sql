-- P55 HxDeploy: Intelligent Deployment Governance

CREATE TABLE environment_registry (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    name            VARCHAR(100) NOT NULL,           -- dev | staging | uat | prod
    label           TEXT        NOT NULL,
    url             TEXT,
    order_index     INTEGER     NOT NULL DEFAULT 0,  -- promotion order: 0=dev, 3=prod
    current_package_id  UUID,
    current_version TEXT,
    status          VARCHAR(50) NOT NULL DEFAULT 'healthy',
    -- healthy | degraded | down | unknown
    last_deployed_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_env_tenant ON environment_registry(tenant_id);
CREATE UNIQUE INDEX ix_env_name_tenant ON environment_registry(tenant_id, name);

CREATE TABLE deployment_runs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    package_id      UUID,
    from_env_id     UUID        REFERENCES environment_registry(id) ON DELETE SET NULL,
    to_env_id       UUID        REFERENCES environment_registry(id) ON DELETE SET NULL,
    risk_level      VARCHAR(20) NOT NULL DEFAULT 'medium',
    -- low | medium | high | critical
    risk_summary    JSONB       NOT NULL DEFAULT '{}',
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | awaiting_approval | approved | rejected | deploying | deployed | failed | rolled_back
    approval_case_id UUID,                           -- linked Work Center case
    approved_by     TEXT,
    rejected_by     TEXT,
    rejection_reason TEXT,
    initiated_by    TEXT        NOT NULL,
    deploy_notes    TEXT,
    deployed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX ix_dr_tenant  ON deployment_runs(tenant_id);
CREATE INDEX ix_dr_status  ON deployment_runs(status);
CREATE INDEX ix_dr_to_env  ON deployment_runs(to_env_id);

CREATE TABLE deployment_windows (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    env_id          UUID        REFERENCES environment_registry(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL,
    days_of_week    JSONB       NOT NULL DEFAULT '[0,1,2,3,4,5,6]', -- 0=Mon..6=Sun
    start_hour_utc  INTEGER     NOT NULL DEFAULT 0,
    end_hour_utc    INTEGER     NOT NULL DEFAULT 23,
    enabled         BOOLEAN     NOT NULL DEFAULT true
);

CREATE INDEX ix_dw_env ON deployment_windows(env_id);

CREATE TABLE deployment_health_checks (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID        NOT NULL REFERENCES deployment_runs(id) ON DELETE CASCADE,
    check_url   TEXT,
    status_code INTEGER,
    response_ms INTEGER,
    healthy     BOOLEAN,
    error       TEXT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_dhc_run ON deployment_health_checks(run_id);
