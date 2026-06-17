-- 085: Group I — RFC-3161 external anchoring of the audit hash chain.
-- Each row stores one TSA receipt (raw DER TimeStampResp) over the chain tip,
-- upgrading the chain from internally tamper-evident to externally provable.

CREATE TABLE IF NOT EXISTS audit_anchors (
    id            UUID         PRIMARY KEY,
    tip_sequence  INTEGER      NOT NULL,           -- chain sequence the receipt covers
    tip_hash      VARCHAR(64)  NOT NULL,           -- content_hash of that chain row
    tsa_url       VARCHAR(512) NOT NULL,           -- authority that issued the receipt
    tsr_der       BYTEA        NOT NULL,           -- raw RFC-3161 TimeStampResp
    anchored_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_anchors_tip_seq ON audit_anchors (tip_sequence);
CREATE INDEX IF NOT EXISTS ix_audit_anchors_anchored_at ON audit_anchors (anchored_at);
