-- R5e step 7 (the commerce split) — one row per case commerce-service asked
-- case-service to open (an order, a return, a complaint).
--
-- commerce-service opens the case over case-service's internal API and then
-- writes the order in its own transaction. The request key makes a retried call
-- return the case the first one opened, and lets case-service's order sweep find
-- an order-case whose order was never written. Indexed and portable — case data
-- (JSON) is never queried inside.
--
-- Additive and re-runnable. The previous release's code never reads it.
CREATE TABLE IF NOT EXISTS commerce_case_requests (
    request_key  VARCHAR(200) PRIMARY KEY,
    kind         VARCHAR(20)  NOT NULL,
    case_id      UUID         NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_commerce_case_requests_case ON commerce_case_requests (case_id);
CREATE INDEX IF NOT EXISTS ix_commerce_case_requests_kind_created ON commerce_case_requests (kind, created_at);
