-- 095: HxCheckout schema — the 6 checkout_* tables for the commerce integration
-- layer (marketplace app `velaris/hxcheckout`).
--
-- HxCheckout's Python ships in-image like every official marketplace module; these
-- tables ship on the normal startup migration track (the marketplace install only
-- flips the per-tenant gate + Studio routes, it does NOT provision schema). The
-- tables are always present, empty until the app is used.
--
-- Idempotent (IF NOT EXISTS). Column types/nullability/defaults/indexes mirror the
-- ORM models in db/models.py exactly (CheckoutOrder*, CheckoutServiceToken,
-- CheckoutWebhookIntegration, CheckoutWebhookEvent, CheckoutNotificationLog).

CREATE TABLE IF NOT EXISTS checkout_webhook_integrations (
    id              UUID         PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    platform        VARCHAR(50)  NOT NULL DEFAULT 'custom',
    label           VARCHAR(255) NOT NULL DEFAULT '',
    hmac_secret_enc TEXT         NULL,
    field_map       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by      VARCHAR(255) NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_checkout_integrations_tenant ON checkout_webhook_integrations (tenant_id);

CREATE TABLE IF NOT EXISTS checkout_orders (
    id                 UUID         PRIMARY KEY,
    tenant_id          VARCHAR(255) NOT NULL,
    case_id            UUID         NULL REFERENCES case_instances(id) ON DELETE SET NULL,
    tracking_token     VARCHAR(64)  NOT NULL,
    status             VARCHAR(50)  NOT NULL DEFAULT 'pending_payment',
    currency           VARCHAR(10)  NOT NULL DEFAULT 'GBP',
    total_cents        BIGINT       NOT NULL DEFAULT 0,
    customer           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    shipping           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    metadata           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    source             VARCHAR(50)  NOT NULL DEFAULT 'api',
    idempotency_key    VARCHAR(255) NULL,
    integration_id     UUID         NULL REFERENCES checkout_webhook_integrations(id) ON DELETE SET NULL,
    payment_request_id UUID         NULL,
    is_test            BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_checkout_orders_tracking UNIQUE (tracking_token),
    -- Idempotent order creation: a retried request with the same Idempotency-Key
    -- returns the existing order. Per tenant; NULLs are distinct (keyless orders).
    CONSTRAINT uq_checkout_orders_idem UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_checkout_orders_tenant ON checkout_orders (tenant_id);
CREATE INDEX IF NOT EXISTS ix_checkout_orders_case   ON checkout_orders (case_id);
CREATE INDEX IF NOT EXISTS ix_checkout_orders_status ON checkout_orders (status);

CREATE TABLE IF NOT EXISTS checkout_order_items (
    id               UUID         PRIMARY KEY,
    order_id         UUID         NOT NULL REFERENCES checkout_orders(id) ON DELETE CASCADE,
    sku              VARCHAR(255) NOT NULL,
    name             VARCHAR(512) NOT NULL,
    quantity         INTEGER      NOT NULL DEFAULT 1,
    unit_price_cents BIGINT       NOT NULL DEFAULT 0,
    metadata         JSONB        NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_checkout_order_items_order ON checkout_order_items (order_id);

CREATE TABLE IF NOT EXISTS checkout_service_tokens (
    id           UUID         PRIMARY KEY,
    tenant_id    VARCHAR(255) NOT NULL,
    label        VARCHAR(255) NOT NULL DEFAULT '',
    token_hash   VARCHAR(255) NOT NULL,
    token_prefix VARCHAR(24)  NOT NULL,
    scope        VARCHAR(50)  NOT NULL DEFAULT 'orders:create',
    last_used_at TIMESTAMPTZ  NULL,
    revoked_at   TIMESTAMPTZ  NULL,
    suspended    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_by   VARCHAR(255) NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- token_prefix carries the public key-id (vsk_<mode>_<keyid>): O(1) auth lookup
    -- since the salted bcrypt token_hash can't be queried directly.
    CONSTRAINT uq_checkout_tokens_prefix UNIQUE (token_prefix)
);
CREATE INDEX IF NOT EXISTS ix_checkout_tokens_tenant ON checkout_service_tokens (tenant_id);

CREATE TABLE IF NOT EXISTS checkout_webhook_events (
    id             UUID         PRIMARY KEY,
    integration_id UUID         NULL REFERENCES checkout_webhook_integrations(id) ON DELETE CASCADE,
    raw            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    mapped         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    status         VARCHAR(50)  NOT NULL DEFAULT 'received',
    order_id       UUID         NULL,
    error          TEXT         NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_checkout_wh_events_integration ON checkout_webhook_events (integration_id);
CREATE INDEX IF NOT EXISTS ix_checkout_wh_events_created     ON checkout_webhook_events (created_at);

CREATE TABLE IF NOT EXISTS checkout_notifications_log (
    id         UUID         PRIMARY KEY,
    order_id   UUID         NOT NULL REFERENCES checkout_orders(id) ON DELETE CASCADE,
    event      VARCHAR(100) NOT NULL,
    channel    VARCHAR(20)  NOT NULL,
    status     VARCHAR(50)  NOT NULL DEFAULT 'sent',
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_checkout_notif_order ON checkout_notifications_log (order_id);
