-- R5c step 11 (d) — HxDR: what happened to the standby link.
--
-- link, unlink and promote are the moments an auditor asks about ("when did this
-- install last have a standby, and when did it switch servers?"). The host tool
-- (standby-velaris.sh) records them through case_service.compliance.standby_cli,
-- the same way backups are recorded (dr_cli). Kept in the database so the
-- history replicates to the standby and survives a switch-over.
--
-- Additive and re-runnable; the previous version's code never reads it.
CREATE TABLE IF NOT EXISTS dr_standby_events (
    id           UUID         PRIMARY KEY,
    event        VARCHAR(32)  NOT NULL,          -- link | unlink | promote
    peer         VARCHAR(255) NULL,              -- the other server
    detail       TEXT         NOT NULL DEFAULT '{}',   -- JSON: states, LSNs, loss, documents
    recorded_by  VARCHAR(64)  NOT NULL DEFAULT 'standby-velaris.sh',
    occurred_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_dr_standby_events_at ON dr_standby_events (occurred_at);
