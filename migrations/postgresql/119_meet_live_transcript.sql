-- HxMeet P4a-live-2: seal the live transcript to the case, like the recording.
--
-- case_session_caption_segments: server-side staging for finalized caption
-- segments while a captioned RECORD-INTENT session runs (speaker identity
-- comes from the verified room token, never from the client). On session end
-- the segments are composed, tenant-DEK sealed, stored as a case document,
-- audit-chain anchored — then the staging rows are DELETED. Non-recorded
-- sessions never stage anything: captions stay ephemeral (consent honesty —
-- joining a record-intent session is the consent act that covers this).
--
-- case_sessions gains the transcript twin of the recording columns.

CREATE TABLE IF NOT EXISTS case_session_caption_segments (
    id         UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES case_sessions(id) ON DELETE CASCADE,
    tenant_id  VARCHAR(255),
    speaker    VARCHAR(255) NOT NULL,   -- verified token identity (user:|customer:|email:)
    text       TEXT NOT NULL,
    spoken_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_caption_segments_session
    ON case_session_caption_segments (session_id, spoken_at);

ALTER TABLE case_sessions
    ADD COLUMN IF NOT EXISTS transcript_status VARCHAR(20) NOT NULL DEFAULT 'none',  -- none|sealed|failed
    ADD COLUMN IF NOT EXISTS transcript_document_id UUID REFERENCES documents(id),
    ADD COLUMN IF NOT EXISTS transcript_anchor_ref TEXT;
