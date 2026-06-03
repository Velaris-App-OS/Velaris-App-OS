-- P46: HxStream — Live Execution & Interaction Stream
-- Creates: trace_events
-- Event types: stage_transition | step_complete | rule_eval | ai_invoke |
--              ui_interaction | notification_sent | queue_route |
--              automation_run | form_submit | integration_call | error

BEGIN;

CREATE TABLE trace_events (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Case context (NULL for system-wide / tenant-level events)
    case_id         UUID        NULL REFERENCES case_instances(id) ON DELETE SET NULL,
    tenant_id       VARCHAR(255) NOT NULL,
    -- Event classification
    event_type      VARCHAR(50) NOT NULL,   -- see comment above
    -- Actor (NULL for automated/system events)
    actor_user_id   VARCHAR(255) NULL,
    actor_ip        VARCHAR(45) NULL,       -- IPv4 or IPv6, populated for ui_interaction events
    -- Event data — structure varies per event_type
    payload         JSONB       NOT NULL DEFAULT '{}',
    -- Timing
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Groups events into one attached trace session
    -- Set by the subscriber when they open an HxStream panel
    session_id      VARCHAR(255) NULL,
    -- Latency captured at emit time (ms) — avoids recomputing in queries
    latency_ms      INTEGER     NULL
);

-- Fast lookup by case (most common query: "show me all events for case X")
CREATE INDEX ix_trace_events_case_id      ON trace_events (case_id, occurred_at DESC);
-- Tenant-scoped queries
CREATE INDEX ix_trace_events_tenant       ON trace_events (tenant_id, occurred_at DESC);
-- Filter by event type
CREATE INDEX ix_trace_events_type         ON trace_events (event_type, occurred_at DESC);
-- Session replay
CREATE INDEX ix_trace_events_session      ON trace_events (session_id, occurred_at ASC)
    WHERE session_id IS NOT NULL;
-- Actor activity feed (who clicked what)
CREATE INDEX ix_trace_events_actor        ON trace_events (actor_user_id, occurred_at DESC)
    WHERE actor_user_id IS NOT NULL;

COMMIT;
