-- P58 HxDocs: Living Documentation

CREATE TABLE hxdocs_spaces (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    slug        TEXT        NOT NULL,
    description TEXT,
    is_public   BOOLEAN     NOT NULL DEFAULT FALSE,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_hxds_tenant ON hxdocs_spaces(tenant_id);
CREATE UNIQUE INDEX ix_hxds_slug ON hxdocs_spaces(tenant_id, slug);

CREATE TABLE hxdocs_articles (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id         UUID        NOT NULL REFERENCES hxdocs_spaces(id) ON DELETE CASCADE,
    tenant_id        TEXT        NOT NULL,
    title            TEXT        NOT NULL,
    slug             TEXT        NOT NULL,
    -- content is an array of blocks: [{id,type,text,...}]
    content          JSONB       NOT NULL DEFAULT '[]',
    status           VARCHAR(20) NOT NULL DEFAULT 'draft',
    -- draft | published
    is_public        BOOLEAN     NOT NULL DEFAULT FALSE,
    auto_generated   BOOLEAN     NOT NULL DEFAULT FALSE,
    source_concept   TEXT,
    word_count       INTEGER     NOT NULL DEFAULT 0,
    version          INTEGER     NOT NULL DEFAULT 1,
    package_version  TEXT,
    tags             JSONB       NOT NULL DEFAULT '[]',
    created_by       TEXT,
    updated_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_hxda_space   ON hxdocs_articles(space_id);
CREATE INDEX ix_hxda_tenant  ON hxdocs_articles(tenant_id);
CREATE INDEX ix_hxda_status  ON hxdocs_articles(status);
CREATE INDEX ix_hxda_public  ON hxdocs_articles(is_public);
CREATE UNIQUE INDEX ix_hxda_slug ON hxdocs_articles(space_id, slug);

CREATE TABLE hxdocs_article_versions (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id  UUID        NOT NULL REFERENCES hxdocs_articles(id) ON DELETE CASCADE,
    tenant_id   TEXT        NOT NULL,
    version     INTEGER     NOT NULL,
    title       TEXT        NOT NULL,
    content     JSONB       NOT NULL,
    package_version TEXT,
    saved_by    TEXT,
    saved_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_hxdav_article ON hxdocs_article_versions(article_id);
CREATE INDEX ix_hxdav_version ON hxdocs_article_versions(version);
