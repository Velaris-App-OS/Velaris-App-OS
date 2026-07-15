-- HxMeet P1: real-time sessions (meetings) attached to a case.
-- One provider-agnostic abstraction, two drivers: off_platform (P1 —
-- Teams/Zoom/Meet/generic connector, recording stays with the provider)
-- and embedded (P2+ — self-hosted LiveKit). recording_document_id /
-- audit_anchor_ref stay NULL until the P3 sealed-recording path.

CREATE TABLE IF NOT EXISTS case_sessions (
    id                    UUID PRIMARY KEY,
    case_id               UUID         NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    tenant_id             VARCHAR(255),
    driver                VARCHAR(20)  NOT NULL DEFAULT 'off_platform',  -- off_platform | embedded
    provider              VARCHAR(50)  NOT NULL,                         -- teams | zoom | gmeet | generic (P2+: livekit)
    connector_id          UUID,                                          -- connector_registry row that created it
    status                VARCHAR(20)  NOT NULL DEFAULT 'created',       -- created | active | ended | cancelled
    title                 VARCHAR(255),
    external_meeting_id   TEXT,
    join_url              TEXT,
    scheduled_at          TIMESTAMPTZ,
    started_by            VARCHAR(255) NOT NULL,
    started_at            TIMESTAMPTZ,
    ended_at              TIMESTAMPTZ,
    recording_document_id UUID REFERENCES documents(id),                 -- P3
    audit_anchor_ref      TEXT,                                          -- P3
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_case_sessions_case   ON case_sessions (case_id);
CREATE INDEX IF NOT EXISTS ix_case_sessions_tenant ON case_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS ix_case_sessions_status ON case_sessions (status);
