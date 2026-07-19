-- HxMeet P4d: distinct biometric consent (GDPR Art. 9 — explicit, separate
-- from the recording consent). Stamped by the same acts that stamp recording
-- consent (worker token mint / customer join / guest exchange), but ONLY when
-- the tenant has opted into biometric matching AND the UI showed the distinct
-- biometric notice first. The face-match check refuses to run over any joined
-- participant without this stamp. Embeddings are computed, compared, and
-- DISCARDED — only the score is ever stored.

ALTER TABLE case_session_participants
    ADD COLUMN IF NOT EXISTS biometric_consent_at TIMESTAMPTZ;
