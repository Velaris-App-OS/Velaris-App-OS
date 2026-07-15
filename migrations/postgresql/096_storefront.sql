-- 096: HxStorefront schema — the 18 storefront_* tables (hosted store builder,
-- marketplace app `velaris/hxstorefront`).
--
-- Same pattern as HxCheckout (095): Python + Studio ship in-image; these tables
-- ship on the normal startup migration track. Install only flips the per-tenant
-- gate + routes. Idempotent (IF NOT EXISTS). Types/nullability/defaults/indexes
-- mirror the ORM models in db/models.py (Storefront*). Created in dependency order.

CREATE TABLE IF NOT EXISTS storefront_stores (
    id         UUID         PRIMARY KEY,
    tenant_id  VARCHAR(255) NOT NULL,
    slug       VARCHAR(255) NOT NULL,
    name       VARCHAR(255) NOT NULL,
    currency   VARCHAR(10)  NOT NULL DEFAULT 'GBP',
    locale     VARCHAR(20)  NOT NULL DEFAULT 'en-GB',
    status     VARCHAR(20)  NOT NULL DEFAULT 'active',
    settings   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_storefront_stores_slug UNIQUE (slug)
);
CREATE INDEX IF NOT EXISTS ix_storefront_stores_tenant ON storefront_stores (tenant_id);

CREATE TABLE IF NOT EXISTS storefront_products (
    id                  UUID         PRIMARY KEY,
    store_id            UUID         NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    name                VARCHAR(512) NOT NULL,
    slug                VARCHAR(255) NOT NULL,
    sku                 VARCHAR(255) NULL,
    description         TEXT         NOT NULL DEFAULT '',
    short_description   VARCHAR(512) NOT NULL DEFAULT '',
    tags                JSONB        NOT NULL DEFAULT '[]'::jsonb,
    price_cents         BIGINT       NOT NULL DEFAULT 0,
    compare_at_cents    BIGINT       NULL,
    tax_class           VARCHAR(20)  NOT NULL DEFAULT 'standard',
    weight_grams        INTEGER      NOT NULL DEFAULT 0,
    status              VARCHAR(20)  NOT NULL DEFAULT 'draft',
    stock_quantity      INTEGER      NULL,
    low_stock_threshold INTEGER      NULL,
    is_featured         BOOLEAN      NOT NULL DEFAULT FALSE,
    is_digital          BOOLEAN      NOT NULL DEFAULT FALSE,
    metadata            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_storefront_products_slug UNIQUE (store_id, slug)
);
CREATE INDEX IF NOT EXISTS ix_storefront_products_store  ON storefront_products (store_id);
CREATE INDEX IF NOT EXISTS ix_storefront_products_status ON storefront_products (status);

CREATE TABLE IF NOT EXISTS storefront_product_images (
    id            UUID         PRIMARY KEY,
    product_id    UUID         NOT NULL REFERENCES storefront_products(id) ON DELETE CASCADE,
    media_path    VARCHAR(1024) NOT NULL,
    alt_text      VARCHAR(512) NOT NULL DEFAULT '',
    display_order INTEGER      NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_storefront_images_product ON storefront_product_images (product_id);

CREATE TABLE IF NOT EXISTS storefront_variant_options (
    id            UUID         PRIMARY KEY,
    product_id    UUID         NOT NULL REFERENCES storefront_products(id) ON DELETE CASCADE,
    name          VARCHAR(255) NOT NULL,
    values        JSONB        NOT NULL DEFAULT '[]'::jsonb,
    display_order INTEGER      NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_storefront_varopt_product ON storefront_variant_options (product_id);

CREATE TABLE IF NOT EXISTS storefront_product_variants (
    id             UUID         PRIMARY KEY,
    product_id     UUID         NOT NULL REFERENCES storefront_products(id) ON DELETE CASCADE,
    sku            VARCHAR(255) NOT NULL,
    option_values  JSONB        NOT NULL DEFAULT '{}'::jsonb,
    price_cents    BIGINT       NULL,
    stock_quantity INTEGER      NULL,
    media_path     VARCHAR(1024) NULL,
    display_order  INTEGER      NOT NULL DEFAULT 0,
    CONSTRAINT uq_storefront_variants_sku UNIQUE (product_id, sku)
);
CREATE INDEX IF NOT EXISTS ix_storefront_variants_product ON storefront_product_variants (product_id);

CREATE TABLE IF NOT EXISTS storefront_categories (
    id            UUID         PRIMARY KEY,
    store_id      UUID         NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    parent_id     UUID         NULL REFERENCES storefront_categories(id) ON DELETE SET NULL,
    name          VARCHAR(255) NOT NULL,
    slug          VARCHAR(255) NOT NULL,
    description   TEXT         NOT NULL DEFAULT '',
    banner_path   VARCHAR(1024) NULL,
    display_order INTEGER      NOT NULL DEFAULT 0,
    CONSTRAINT uq_storefront_categories_slug UNIQUE (store_id, slug)
);
CREATE INDEX IF NOT EXISTS ix_storefront_categories_store  ON storefront_categories (store_id);
CREATE INDEX IF NOT EXISTS ix_storefront_categories_parent ON storefront_categories (parent_id);

CREATE TABLE IF NOT EXISTS storefront_product_categories (
    id          UUID PRIMARY KEY,
    product_id  UUID NOT NULL REFERENCES storefront_products(id) ON DELETE CASCADE,
    category_id UUID NOT NULL REFERENCES storefront_categories(id) ON DELETE CASCADE,
    CONSTRAINT uq_storefront_prodcat UNIQUE (product_id, category_id)
);
CREATE INDEX IF NOT EXISTS ix_storefront_prodcat_product  ON storefront_product_categories (product_id);
CREATE INDEX IF NOT EXISTS ix_storefront_prodcat_category ON storefront_product_categories (category_id);

CREATE TABLE IF NOT EXISTS storefront_inventory_log (
    id           UUID         PRIMARY KEY,
    variant_id   UUID         NOT NULL REFERENCES storefront_product_variants(id) ON DELETE CASCADE,
    change       INTEGER      NOT NULL,
    new_quantity INTEGER      NULL,
    reason       VARCHAR(100) NOT NULL DEFAULT 'adjustment',
    actor        VARCHAR(255) NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_storefront_invlog_variant ON storefront_inventory_log (variant_id);

CREATE TABLE IF NOT EXISTS storefront_themes (
    id         UUID        PRIMARY KEY,
    store_id   UUID        NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    config     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    version    INTEGER     NOT NULL DEFAULT 1,
    is_active  BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_storefront_themes_store ON storefront_themes (store_id);

CREATE TABLE IF NOT EXISTS storefront_pages (
    id           UUID         PRIMARY KEY,
    store_id     UUID         NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    page_slug    VARCHAR(255) NOT NULL,
    title        VARCHAR(512) NOT NULL DEFAULT '',
    sections     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    is_published BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_storefront_pages_slug UNIQUE (store_id, page_slug)
);
CREATE INDEX IF NOT EXISTS ix_storefront_pages_store ON storefront_pages (store_id);

CREATE TABLE IF NOT EXISTS storefront_navigation (
    id         UUID        PRIMARY KEY,
    store_id   UUID        NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    location   VARCHAR(20) NOT NULL DEFAULT 'header',
    items      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_storefront_nav_location UNIQUE (store_id, location)
);

CREATE TABLE IF NOT EXISTS storefront_promotions (
    id                 UUID         PRIMARY KEY,
    store_id           UUID         NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    code               VARCHAR(64)  NULL,
    discount_type      VARCHAR(30)  NOT NULL,
    config             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    applies_to         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    min_order_cents    BIGINT       NULL,
    usage_limit        INTEGER      NULL,
    per_customer_limit INTEGER      NULL,
    valid_from         TIMESTAMPTZ  NULL,
    valid_until        TIMESTAMPTZ  NULL,
    stackable          BOOLEAN      NOT NULL DEFAULT FALSE,
    active             BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_storefront_promotions_store ON storefront_promotions (store_id);
CREATE INDEX IF NOT EXISTS ix_storefront_promotions_code  ON storefront_promotions (code);

CREATE TABLE IF NOT EXISTS storefront_promotion_uses (
    id             UUID         PRIMARY KEY,
    promotion_id   UUID         NOT NULL REFERENCES storefront_promotions(id) ON DELETE CASCADE,
    order_ref      VARCHAR(255) NULL,
    customer_email VARCHAR(255) NULL,
    used_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_storefront_promouse_promo ON storefront_promotion_uses (promotion_id);

CREATE TABLE IF NOT EXISTS storefront_domains (
    id           UUID         PRIMARY KEY,
    store_id     UUID         NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    domain       VARCHAR(255) NOT NULL,
    domain_type  VARCHAR(20)  NOT NULL DEFAULT 'cname',
    dns_verified BOOLEAN      NOT NULL DEFAULT FALSE,
    ssl_status   VARCHAR(20)  NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_storefront_domains_domain UNIQUE (domain)
);
CREATE INDEX IF NOT EXISTS ix_storefront_domains_store ON storefront_domains (store_id);

CREATE TABLE IF NOT EXISTS storefront_seo_overrides (
    id               UUID         PRIMARY KEY,
    store_id         UUID         NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    target_type      VARCHAR(20)  NOT NULL,
    target_id        VARCHAR(255) NOT NULL,
    meta_title       VARCHAR(255) NOT NULL DEFAULT '',
    meta_description VARCHAR(512) NOT NULL DEFAULT '',
    og_title         VARCHAR(255) NOT NULL DEFAULT '',
    og_description   VARCHAR(512) NOT NULL DEFAULT '',
    og_image         VARCHAR(1024) NULL,
    canonical_url    VARCHAR(1024) NULL,
    CONSTRAINT uq_storefront_seo_target UNIQUE (store_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS storefront_subscribers (
    id         UUID         PRIMARY KEY,
    store_id   UUID         NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    email      VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_storefront_subscribers_email UNIQUE (store_id, email)
);

CREATE TABLE IF NOT EXISTS storefront_media (
    id         UUID          PRIMARY KEY,
    store_id   UUID          NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    media_path VARCHAR(1024) NOT NULL,
    media_type VARCHAR(50)   NOT NULL DEFAULT 'image',
    size_bytes BIGINT        NOT NULL DEFAULT 0,
    alt_text   VARCHAR(512)  NOT NULL DEFAULT '',
    folder     VARCHAR(512)  NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_storefront_media_store ON storefront_media (store_id);

CREATE TABLE IF NOT EXISTS storefront_analytics_events (
    id         UUID        PRIMARY KEY,
    store_id   UUID        NOT NULL REFERENCES storefront_stores(id) ON DELETE CASCADE,
    event      VARCHAR(50) NOT NULL,
    data       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    session    VARCHAR(128) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_storefront_events_store   ON storefront_analytics_events (store_id);
CREATE INDEX IF NOT EXISTS ix_storefront_events_created ON storefront_analytics_events (created_at);
