-- HxMeet P3: sealed recording columns on case_sessions.
-- record_intent is declared at session start; every subsequent join stamps
-- per-participant consent (case_session_participants.consent_recorded_at,
-- mig 113). recording_status: none > recording > processing > sealed|failed.
-- recording_document_id / audit_anchor_ref (mig 112) get their first writes.

ALTER TABLE case_sessions ADD COLUMN IF NOT EXISTS record_intent       BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE case_sessions ADD COLUMN IF NOT EXISTS recording_status    VARCHAR(20) NOT NULL DEFAULT 'none';
ALTER TABLE case_sessions ADD COLUMN IF NOT EXISTS recording_egress_id TEXT;
