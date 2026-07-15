-- HELIX Phase 8 Migration
-- Adds: soft-delete columns, business_calendar table
BEGIN;

-- Soft-delete columns on case_types
ALTER TABLE case_types ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT false;
ALTER TABLE case_types ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE case_types ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255);

-- Business calendar table
CREATE TABLE IF NOT EXISTS business_calendars (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL UNIQUE,
    timezone        VARCHAR(100) DEFAULT 'UTC',
    work_days       INT[] DEFAULT '{1,2,3,4,5}',
    work_start_hour INT DEFAULT 9,
    work_end_hour   INT DEFAULT 17,
    holidays        JSONB DEFAULT '[]',
    description     TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Link SLA policies to a business calendar
ALTER TABLE case_sla_instances ADD COLUMN IF NOT EXISTS calendar_id UUID REFERENCES business_calendars(id);

-- Insert default calendar
INSERT INTO business_calendars (name, timezone, work_days, work_start_hour, work_end_hour, holidays, description)
VALUES ('default', 'UTC', '{1,2,3,4,5}', 9, 17, '[]', 'Default business calendar: Mon-Fri 9-17 UTC')
ON CONFLICT (name) DO NOTHING;

COMMIT;
