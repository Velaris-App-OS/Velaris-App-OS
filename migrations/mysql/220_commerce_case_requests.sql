-- R5e step 7 — one row per case commerce-service asked case-service to open.
-- See migrations/postgresql/220_commerce_case_requests.sql for the reasoning.
-- Additive and re-runnable. Dialect notes: UUID → CHAR(36).
CREATE TABLE IF NOT EXISTS commerce_case_requests (
    request_key  VARCHAR(200) PRIMARY KEY,
    kind         VARCHAR(20)  NOT NULL,
    case_id      CHAR(36)     NOT NULL,
    created_at   DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_commerce_case_requests_case (case_id),
    INDEX ix_commerce_case_requests_kind_created (kind, created_at)
);
