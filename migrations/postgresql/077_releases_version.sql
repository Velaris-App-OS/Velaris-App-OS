-- Migration 077: Add version column to scheduled_releases
ALTER TABLE scheduled_releases
    ADD COLUMN IF NOT EXISTS version VARCHAR(32);
