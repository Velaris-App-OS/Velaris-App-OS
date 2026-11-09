-- R5e step 9 (Nov_TODO) — one lease per background job, so a job never runs in
-- two processes at once.
--
-- case-service starts its background jobs (the scheduler, the email poll, the
-- chain sealers, the sweeps and watchers) in every process, and almost none of
-- them guard against a second copy: two processes fire a timed action twice,
-- ingest a mailbox twice, notify an admin twice. That is why every service runs
-- one worker today, and why two case-service replicas on Kubernetes already run
-- every job twice. A job now runs only while its process holds the job's row
-- here: taken when free or expired, renewed while running, released on stop.
-- Plain rows and conditional UPDATEs — no session advisory locks, which a
-- transaction-pooling PgBouncer does not carry, and the same on MySQL.
--
-- Additive and re-runnable. The previous release's code never reads it.
CREATE TABLE IF NOT EXISTS job_leases (
    job          VARCHAR(80)  PRIMARY KEY,
    holder       VARCHAR(200) NOT NULL,
    expires_at   TIMESTAMPTZ  NOT NULL,
    acquired_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    renewed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
