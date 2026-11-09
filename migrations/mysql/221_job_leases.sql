-- R5e step 9 — one lease per background job, so a job never runs in two
-- processes at once. See migrations/postgresql/221_job_leases.sql for the
-- reasoning. Additive and re-runnable.
CREATE TABLE IF NOT EXISTS job_leases (
    job          VARCHAR(80)  PRIMARY KEY,
    holder       VARCHAR(200) NOT NULL,
    expires_at   DATETIME(6)  NOT NULL,
    acquired_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    renewed_at   DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
);
