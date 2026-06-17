-- 084: PUO Phase 3 — platform update rollout plans (fleet orchestration).
-- Sibling of HxDeploy's deployment_runs: these orchestrate PLATFORM CODE
-- updates across registered environments; deployment_runs promote Studio
-- artifacts. Deliberately separate tables.

CREATE TABLE IF NOT EXISTS platform_update_plans (
    id                UUID         PRIMARY KEY,
    resolved_version  TEXT         NOT NULL,             -- stamped at activation (supersede checks compare manifest vs this)
    channel           TEXT         NOT NULL DEFAULT 'stable',
    soak_hours        INTEGER      NOT NULL DEFAULT 48,
    state             TEXT         NOT NULL DEFAULT 'draft',
        -- draft | active | soaking | awaiting_prod_approval | prod_approved
        -- | completed | halted | superseded
    halted_reason     TEXT,
    approved_by       TEXT,
    approved_at       TIMESTAMPTZ,
    prod_approved_by  TEXT,
    prod_approved_at  TIMESTAMPTZ,
    soak_started_at   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_puo_plans_state ON platform_update_plans (state);

CREATE TABLE IF NOT EXISTS platform_update_runs (
    id              UUID         PRIMARY KEY,
    plan_id         UUID         NOT NULL REFERENCES platform_update_plans(id) ON DELETE CASCADE,
    environment_id  UUID         NOT NULL REFERENCES environment_registry(id) ON DELETE CASCADE,
    ring_order      INTEGER      NOT NULL DEFAULT 0,
    is_final_ring   BOOLEAN      NOT NULL DEFAULT FALSE,  -- the prod ring: soak + second approval gate
    state           TEXT         NOT NULL DEFAULT 'pending',
        -- pending | triggered | running | succeeded | failed | halted
    detail          TEXT,
    triggered_at    TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_puo_runs_plan ON platform_update_runs (plan_id);

CREATE TABLE IF NOT EXISTS platform_update_settings (
    id                 INTEGER     PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- single row
    mode               TEXT        NOT NULL DEFAULT 'auto-soak',  -- auto-soak | per-env | manual
    default_soak_hours INTEGER     NOT NULL DEFAULT 48,
    calendar_id        UUID,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO platform_update_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
