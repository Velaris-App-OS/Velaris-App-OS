-- HxMeet P4a/P4b: session intelligence + document verification.
--
-- case_session_intelligence: one analysis run per session (re-run replaces).
-- Local-only pipeline: Whisper transcript + HxNexus summary; the transcript is
-- stored as a case document (document_id), scores/versions stay here so old
-- results remain interpretable after model upgrades.
--
-- document_verifications: the P4b document-first gate — automated checks +
-- worker checklist on an uploaded ID/document, recorded per document. The
-- video-KYC step stays locked until a passing record exists (stage ordering
-- does the locking; this is the evidence).

CREATE TABLE IF NOT EXISTS case_session_intelligence (
    session_id             UUID PRIMARY KEY REFERENCES case_sessions(id) ON DELETE CASCADE,
    status                 VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|running|completed|failed
    transcript_document_id UUID,
    summary                TEXT,
    action_items           JSONB NOT NULL DEFAULT '[]',
    language               VARCHAR(16),
    duration_seconds       INTEGER,
    model_versions         JSONB NOT NULL DEFAULT '{}',
    error                  TEXT,
    requested_by           VARCHAR(255) NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at           TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS document_verifications (
    id           UUID PRIMARY KEY,
    case_id      UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status       VARCHAR(20) NOT NULL DEFAULT 'review',  -- passed|failed|review
    checks       JSONB NOT NULL DEFAULT '[]',            -- [{name,result,detail}]
    verified_by  VARCHAR(255) NOT NULL,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doc_verifications_case
    ON document_verifications (case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_doc_verifications_doc
    ON document_verifications (document_id);
