-- Fix: create shield_events table for P59 HxShield
-- Migration 047 conflicted with Phase 20's security_events table.
-- This migration cleans up the spurious index and creates the correct table.

-- Drop the orphaned shield index that was partially applied to the Phase 20 table
DROP INDEX IF EXISTS ix_shield_ev_type;
DROP INDEX IF EXISTS ix_shield_ev_actor;
DROP INDEX IF EXISTS ix_shield_ev_recorded;

CREATE TABLE IF NOT EXISTS shield_events (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type       VARCHAR(50)  NOT NULL,
    actor_id         VARCHAR(255),
    tenant_id        VARCHAR(255),
    case_type_id     VARCHAR(255),
    payload_hash     VARCHAR(64),
    score            FLOAT        NOT NULL DEFAULT 0.0,
    patterns_matched JSONB        NOT NULL DEFAULT '[]',
    raw_context      JSONB        NOT NULL DEFAULT '{}',
    recorded_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_shield_ev_actor    ON shield_events (actor_id);
CREATE INDEX IF NOT EXISTS ix_shield_ev_type     ON shield_events (event_type);
CREATE INDEX IF NOT EXISTS ix_shield_ev_recorded ON shield_events (recorded_at DESC);
