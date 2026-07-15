-- P43: App Export & Environment Pipeline
-- app_packages: versioned snapshots of the full platform state
-- app_deployments: promotion history across environments

BEGIN;

CREATE TABLE app_packages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    version         VARCHAR(50)  NOT NULL,
    description     TEXT,
    bundle          JSONB        NOT NULL DEFAULT '{}',  -- full snapshot
    manifest        JSONB        NOT NULL DEFAULT '{}',  -- counts + checksums
    status          VARCHAR(20)  NOT NULL DEFAULT 'draft', -- draft|published|deprecated
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ix_app_packages_name_version ON app_packages(name, version);
CREATE INDEX        ix_app_packages_status       ON app_packages(status);
CREATE INDEX        ix_app_packages_created      ON app_packages(created_at DESC);

CREATE TABLE app_deployments (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    package_id       UUID        NOT NULL REFERENCES app_packages(id) ON DELETE CASCADE,
    environment      VARCHAR(50) NOT NULL,   -- dev | staging | uat | prod
    status           VARCHAR(20) NOT NULL DEFAULT 'deployed', -- deployed|rolled_back|failed
    deployed_by      VARCHAR(255),
    deployed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes            TEXT,
    config_overrides JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX ix_app_deployments_package     ON app_deployments(package_id);
CREATE INDEX ix_app_deployments_environment ON app_deployments(environment);
CREATE INDEX ix_app_deployments_deployed    ON app_deployments(deployed_at DESC);

COMMIT;
