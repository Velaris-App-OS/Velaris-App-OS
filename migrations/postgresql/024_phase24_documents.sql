-- HELIX P24 — Document Management
CREATE TABLE IF NOT EXISTS documents (
    id              UUID PRIMARY KEY,
    case_id         UUID NOT NULL REFERENCES case_instances(id),
    filename        VARCHAR(512) NOT NULL,
    content_type    VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream',
    current_version INTEGER NOT NULL DEFAULT 1,
    uploaded_by     VARCHAR(255),
    tenant_id       VARCHAR(64),
    tags            JSONB NOT NULL DEFAULT '{}'::jsonb,
    ocr_text        TEXT,
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at      TIMESTAMPTZ,
    deleted_by      VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_documents_case    ON documents(case_id);
CREATE INDEX IF NOT EXISTS idx_documents_tenant  ON documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_documents_deleted ON documents(is_deleted);

CREATE TABLE IF NOT EXISTS document_versions (
    id            UUID PRIMARY KEY,
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    storage_key   VARCHAR(1024) NOT NULL,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    sha256        VARCHAR(64) NOT NULL DEFAULT '',
    uploaded_by   VARCHAR(255),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_document_version UNIQUE (document_id, version)
);
CREATE INDEX IF NOT EXISTS idx_document_versions_doc ON document_versions(document_id);
