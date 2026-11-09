-- R5c step 11 (d) — HxDR: what happened to the standby link. See
-- migrations/postgresql/222_dr_standby_events.sql for the reasoning.
-- Additive and re-runnable.
CREATE TABLE IF NOT EXISTS dr_standby_events (
    id           CHAR(36)     PRIMARY KEY,
    event        VARCHAR(32)  NOT NULL,
    peer         VARCHAR(255) NULL,
    detail       TEXT         NOT NULL,
    recorded_by  VARCHAR(64)  NOT NULL DEFAULT 'standby-velaris.sh',
    occurred_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_dr_standby_events_at (occurred_at)
);
