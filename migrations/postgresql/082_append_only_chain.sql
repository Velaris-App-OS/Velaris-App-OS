-- 082: Enforce physical append-only integrity on case_audit_log_chain
--
-- The unique constraint on sequence already exists (migration 027).
-- This migration adds DB-level triggers that prevent UPDATE and DELETE
-- on any sealed chain row, making tampering detectable even with direct
-- DB access. The trigger fires as part of the statement — it cannot be
-- bypassed by application-level code without altering the trigger itself.

CREATE OR REPLACE FUNCTION fn_audit_chain_immutable()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'case_audit_log_chain rows are immutable (% attempted on sequence %)',
        TG_OP, OLD.sequence;
END;
$$;

CREATE OR REPLACE TRIGGER tg_audit_chain_no_update
    BEFORE UPDATE ON case_audit_log_chain
    FOR EACH ROW EXECUTE FUNCTION fn_audit_chain_immutable();

CREATE OR REPLACE TRIGGER tg_audit_chain_no_delete
    BEFORE DELETE ON case_audit_log_chain
    FOR EACH ROW EXECUTE FUNCTION fn_audit_chain_immutable();
