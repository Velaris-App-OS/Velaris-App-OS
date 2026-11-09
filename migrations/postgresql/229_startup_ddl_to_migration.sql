-- 229 — move case-service's start-up DDL into a migration (R6 soak S9, 2026-11-03).
--
-- WHY. case-service ran three inline blocks at every start ("Migration
-- 042/043/044", from before the migration runner): 5 columns on case_types, 3
-- on artifact_branches, tables intake_events + branch_audit_events and their
-- indexes. `ADD COLUMN IF NOT EXISTS` is a no-op on an install that has them,
-- but PostgreSQL takes the ACCESS EXCLUSIVE lock BEFORE checking. On the soak
-- box the ALTER queued behind long queries, case-service never became healthy,
-- every later read of case_types queued behind the ALTER, and the update
-- halted. On PostgreSQL those blocks were the ONLY definition of these objects;
-- this file is now that definition and the start-up block is gone.
--
-- LIVE-SAFE. Every statement is guarded by a catalogue check, so on an install
-- that already has the objects (every existing one — case-service created them
-- at start) this file takes NO lock on case_types or artifact_branches. Only a
-- missing object is created. Additive: no data, no column or table removed.
-- The previous release's code is unaffected (it still runs its own no-op DDL).
--
-- RE-RUNNABLE. Every step checks first.
--
-- MySQL: its baseline (000_baseline_pg154.sql) already carries all of these;
-- no sibling file is needed.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

DO $$
DECLARE
    _col RECORD;
BEGIN
    -- old 042: artifact_branches v2 ownership columns
    FOR _col IN
        SELECT * FROM (VALUES
            ('artifact_branches', 'owner_id',             'TEXT'),
            ('artifact_branches', 'assigned_reviewer_id', 'TEXT'),
            ('artifact_branches', 'access_group_id',      'UUID'),
            -- old 044: intake trigger fields on case_types
            ('case_types', 'intake_trigger',        'VARCHAR(20) NOT NULL DEFAULT ''manual'''),
            ('case_types', 'trigger_connector_id',  'UUID'),
            ('case_types', 'filter_conditions',     'JSONB NOT NULL DEFAULT ''{}'''),
            ('case_types', 'field_mapping',         'JSONB NOT NULL DEFAULT ''{}'''),
            ('case_types', 'process_definition_id', 'UUID')
        ) AS c(tbl, col, typ)
    LOOP
        IF to_regclass('public.' || _col.tbl) IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = _col.tbl
                  AND column_name = _col.col)
        THEN
            EXECUTE format('ALTER TABLE %I ADD COLUMN %I %s', _col.tbl, _col.col, _col.typ);
        END IF;
    END LOOP;

    -- old 043: branch_audit_events
    IF to_regclass('public.branch_audit_events') IS NULL THEN
        CREATE TABLE branch_audit_events (
            id UUID PRIMARY KEY,
            branch_id UUID NOT NULL,
            event_type VARCHAR(60) NOT NULL,
            actor_id TEXT,
            actor_name TEXT,
            metadata JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    END IF;
    IF to_regclass('public.ix_bae_branch') IS NULL THEN
        CREATE INDEX ix_bae_branch ON branch_audit_events (branch_id);
    END IF;
    IF to_regclass('public.ix_bae_created') IS NULL THEN
        CREATE INDEX ix_bae_created ON branch_audit_events (created_at);
    END IF;

    -- old 044: intake_events
    IF to_regclass('public.intake_events') IS NULL THEN
        CREATE TABLE intake_events (
            id UUID PRIMARY KEY,
            case_type_id UUID REFERENCES case_types(id) ON DELETE SET NULL,
            connector_id UUID,
            source_ip VARCHAR(50),
            raw_payload JSONB NOT NULL DEFAULT '{}',
            status VARCHAR(20) NOT NULL DEFAULT 'received',
            filter_result JSONB NOT NULL DEFAULT '{}',
            created_case_id UUID,
            process_instance_id UUID,
            error TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processed_at TIMESTAMPTZ
        );
    END IF;
    IF to_regclass('public.ix_intake_case_type') IS NULL THEN
        CREATE INDEX ix_intake_case_type ON intake_events (case_type_id);
    END IF;
    IF to_regclass('public.ix_intake_status') IS NULL THEN
        CREATE INDEX ix_intake_status ON intake_events (status);
    END IF;
    IF to_regclass('public.ix_intake_received') IS NULL THEN
        CREATE INDEX ix_intake_received ON intake_events (received_at);
    END IF;
    IF to_regclass('public.ix_intake_case') IS NULL THEN
        CREATE INDEX ix_intake_case ON intake_events (created_case_id);
    END IF;
END
$$;
