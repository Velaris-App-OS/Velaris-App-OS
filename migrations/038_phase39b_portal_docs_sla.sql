-- P39b: Two-Way Document Exchange + SLA Countdown
-- Adds portal_visible and portal_source to documents table.
-- portal_visible: staff toggles to share a document with the customer portal.
-- portal_source:  'customer' for uploads from portal, 'staff' for staff-shared docs.

BEGIN;

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS portal_visible BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS portal_source  VARCHAR(20) NULL;

CREATE INDEX IF NOT EXISTS idx_documents_portal ON documents (case_id, portal_visible)
    WHERE portal_visible = TRUE;

COMMIT;
