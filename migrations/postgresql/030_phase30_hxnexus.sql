-- Migration 030 — P30 HxNexus AI Copilot
-- Tables: hxnexus_document_chunks, hxnexus_conversations, hxnexus_messages

CREATE TABLE IF NOT EXISTS hxnexus_document_chunks (
    id            UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID,
    case_id       UUID,
    chunk_index   INTEGER NOT NULL DEFAULT 0,
    chunk_text    TEXT    NOT NULL,
    embedding     JSONB   NOT NULL DEFAULT '[]',
    tenant_id     VARCHAR(64),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hxnexus_chunk_document ON hxnexus_document_chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_hxnexus_chunk_case     ON hxnexus_document_chunks (case_id);
CREATE INDEX IF NOT EXISTS idx_hxnexus_chunk_tenant   ON hxnexus_document_chunks (tenant_id);

CREATE TABLE IF NOT EXISTS hxnexus_conversations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    VARCHAR(255) NOT NULL,
    case_id    UUID,
    tenant_id  VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hxnexus_conv_user ON hxnexus_conversations (user_id);
CREATE INDEX IF NOT EXISTS idx_hxnexus_conv_case ON hxnexus_conversations (case_id);

CREATE TABLE IF NOT EXISTS hxnexus_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES hxnexus_conversations(id) ON DELETE CASCADE,
    role            VARCHAR(16) NOT NULL,
    content         TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hxnexus_msg_conv ON hxnexus_messages (conversation_id);
