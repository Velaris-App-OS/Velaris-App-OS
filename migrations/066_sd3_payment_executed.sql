-- SD-3: Track actual payment execution separate from authorisation
-- Renames the intent step ("Confirm Payment") from authorisation to execution tracking.
ALTER TABLE payment_disbursements
    ADD COLUMN IF NOT EXISTS disbursement_executed     BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS disbursement_executed_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW();
