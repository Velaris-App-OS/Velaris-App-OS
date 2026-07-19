-- HxMeet P4c: Video-KYC — randomized liveness challenges + passive signal pass.
--
-- case_session_challenges: server-minted randomized challenge script for a
-- record-intent session (head turn / phrase read-back / document tilt). The
-- challenge and its random payload are generated server-side so the guest can
-- never predict them; issuing and the guest's response are both captured in
-- the sealed recording. The worker records the observed result per challenge
-- (human judgment, never automated).
--
-- case_session_kyc_signals: one passive-signal analysis run per session
-- (re-run replaces) over the SEALED recording — screen-replay/moiré, blink &
-- micro-movement, lip-sync alignment, audio-spoof — plus deterministic
-- cross-checks of challenge payloads against the sealed live transcript.
-- Output is a risk score with a per-check breakdown, labelled assistive:
-- the worker records the KYC verdict, the AI never auto-passes.

CREATE TABLE IF NOT EXISTS case_session_challenges (
    id           UUID PRIMARY KEY,
    session_id   UUID NOT NULL REFERENCES case_sessions(id) ON DELETE CASCADE,
    tenant_id    VARCHAR(255),
    kind         VARCHAR(32) NOT NULL,          -- head_turn|phrase_readback|document_tilt
    payload      JSONB NOT NULL DEFAULT '{}',   -- randomized instruction detail (e.g. {"phrase":"4 9 3 2 1 7"})
    issued_by    VARCHAR(255) NOT NULL,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    result       VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|passed|failed|skipped
    result_notes TEXT,
    result_by    VARCHAR(255),
    result_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_session_challenges_session
    ON case_session_challenges (session_id, issued_at);

CREATE TABLE IF NOT EXISTS case_session_kyc_signals (
    session_id       UUID PRIMARY KEY REFERENCES case_sessions(id) ON DELETE CASCADE,
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|running|completed|failed
    risk_score       DOUBLE PRECISION,           -- 0..1, higher = riskier; assistive, never a verdict
    checks           JSONB NOT NULL DEFAULT '[]',    -- [{name,score,detail,model,skipped}]
    challenge_checks JSONB NOT NULL DEFAULT '[]',    -- deterministic transcript cross-checks
    model_versions   JSONB NOT NULL DEFAULT '{}',
    error            TEXT,
    requested_by     VARCHAR(255) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ
);
