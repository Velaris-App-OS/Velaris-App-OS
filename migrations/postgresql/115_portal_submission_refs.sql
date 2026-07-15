-- Portal v2 P2: offline-submission idempotency.
-- A PWA submission carries a client-generated UUID (client_ref); replayed
-- syncs (double flush, two tabs, background-sync retry) resolve to the
-- original case instead of creating a duplicate. Rows are prunable after
-- 30 days — the ref only needs to outlive plausible retry windows.

CREATE TABLE IF NOT EXISTS portal_submission_refs (
    client_ref  UUID PRIMARY KEY,
    tenant_slug VARCHAR(255) NOT NULL,
    case_id     UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_portal_submission_refs_created
    ON portal_submission_refs (created_at);
