-- P52 HxConnect: Document Intelligence & Storage

CREATE TABLE doc_extraction_jobs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    provider        VARCHAR(50) NOT NULL DEFAULT 'docling',
    document_id     UUID,                          -- reference to case_documents.id (if linked)
    document_name   TEXT,
    source_url      TEXT,                          -- presigned or internal URL of doc to parse
    extracted_fields JSONB      NOT NULL DEFAULT '{}',
    raw_text        TEXT,
    confidence      FLOAT,
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | processing | completed | failed
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX ix_docex_case   ON doc_extraction_jobs(case_id);
CREATE INDEX ix_docex_status ON doc_extraction_jobs(status);

CREATE TABLE doc_storage_routes (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT        NOT NULL,
    case_id         UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    step_id         TEXT        NOT NULL,
    connector_id    UUID        REFERENCES connector_registry(id) ON DELETE SET NULL,
    provider        VARCHAR(50) NOT NULL DEFAULT 's3',
    document_name   TEXT        NOT NULL,
    bucket          TEXT,
    object_key      TEXT,
    storage_url     TEXT,                          -- full URL of stored object
    presigned_url   TEXT,                          -- time-limited download link
    size_bytes      BIGINT,
    content_type    VARCHAR(255),
    status          VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending | uploaded | failed
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    uploaded_at     TIMESTAMPTZ
);

CREATE INDEX ix_docst_case   ON doc_storage_routes(case_id);
CREATE INDEX ix_docst_status ON doc_storage_routes(status);
