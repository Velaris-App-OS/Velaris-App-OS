-- HxReplay §11 P2: manual timers / timesheets (billable effort capture).
-- Event-log durations measure wall-clock; these entries measure billable
-- EFFORT (a worker juggling several cases logs what each actually took).
-- cost = billable time × role rate (rate_cards, HxGuard costing.rates gated).

CREATE TABLE IF NOT EXISTS case_time_entries (
    id               UUID PRIMARY KEY,
    tenant_id        VARCHAR(255),
    case_id          UUID         NOT NULL,
    user_id          VARCHAR(255) NOT NULL,
    role             VARCHAR(100),                      -- rate-card lookup; NULL = tenant default '*'
    source           VARCHAR(20)  NOT NULL DEFAULT 'timesheet',  -- timer | timesheet
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,                       -- NULL + source=timer = running
    duration_seconds INTEGER      NOT NULL DEFAULT 0,
    billable         BOOLEAN      NOT NULL DEFAULT TRUE,
    note             TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_time_entries_case   ON case_time_entries (case_id);
CREATE INDEX IF NOT EXISTS ix_time_entries_user   ON case_time_entries (user_id);
CREATE INDEX IF NOT EXISTS ix_time_entries_tenant ON case_time_entries (tenant_id);
