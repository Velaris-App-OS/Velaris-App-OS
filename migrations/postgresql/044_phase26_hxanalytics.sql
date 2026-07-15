-- P26: HxAnalytics — Semantic Business Intelligence
-- saved_reports:        user-defined report definitions (query + chart config)
-- report_subscriptions: scheduled export delivery (email/webhook)

BEGIN;

CREATE TABLE saved_reports (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(255) NOT NULL,
    description  TEXT,
    query_type   VARCHAR(20)  NOT NULL DEFAULT 'structured',  -- 'structured' | 'nl'
    query_def    JSONB        NOT NULL DEFAULT '{}',
    chart_type   VARCHAR(30)  NOT NULL DEFAULT 'bar',         -- bar|line|pie|table|number|funnel
    created_by   VARCHAR(255),
    tenant_id    VARCHAR(255),
    is_public    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_saved_reports_tenant   ON saved_reports(tenant_id);
CREATE INDEX ix_saved_reports_public   ON saved_reports(is_public);
CREATE INDEX ix_saved_reports_created  ON saved_reports(created_at DESC);


CREATE TABLE report_subscriptions (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id       UUID         REFERENCES saved_reports(id) ON DELETE CASCADE,
    delivery_type   VARCHAR(20)  NOT NULL DEFAULT 'email',  -- 'email' | 'webhook'
    destination     VARCHAR(500) NOT NULL,
    schedule        VARCHAR(50)  NOT NULL DEFAULT 'daily',  -- 'daily' | 'weekly' | 'monthly'
    format          VARCHAR(10)  NOT NULL DEFAULT 'csv',    -- 'csv' | 'json' | 'pdf'
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    last_sent_at    TIMESTAMPTZ,
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_report_subs_report  ON report_subscriptions(report_id);
CREATE INDEX ix_report_subs_enabled ON report_subscriptions(enabled);

COMMIT;
