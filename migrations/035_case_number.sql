-- Human-readable case identifiers: HLX-{TYPE}-{NNNNNN}
-- e.g. HLX-CLM-000001, HLX-SUP-000042

BEGIN;

CREATE SEQUENCE IF NOT EXISTS helix_case_seq START 1 INCREMENT 1;

ALTER TABLE case_instances
    ADD COLUMN IF NOT EXISTS case_number VARCHAR(30) UNIQUE;

CREATE INDEX IF NOT EXISTS idx_case_instances_case_number
    ON case_instances (case_number);

COMMIT;
