-- 218 — legal hold release: optional second-person approval (2026-10-14).
--
-- WHY. Releasing a legal hold is what lets a record be destroyed again, and
-- until now one admin could do it alone. A tenant may now name an access group
-- (its compliance team) whose member must approve a release that someone else
-- asked for.
--
-- WHAT DOES NOT CHANGE. Additive: two new tables, nothing altered. A tenant
-- with no row in `legal_hold_settings` (every tenant, on upgrade) releases
-- exactly as before — one admin, at once. `legal_holds` is untouched, so the
-- previous version's code runs unchanged while this applies.
--
-- `approver_group_id` is deliberately NOT a foreign key: deleting the group
-- must not silently switch the rule off (ON DELETE SET NULL would). A group
-- that is gone or inactive makes releases wait, and the settings screen says
-- why; the platform superadmin can re-point it.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS legal_hold_settings (
    tenant_id         UUID         PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    approver_group_id UUID         NULL,
    updated_by        VARCHAR(255) NOT NULL,
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS legal_hold_release_requests (
    id                UUID         PRIMARY KEY,
    hold_id           UUID         NOT NULL REFERENCES legal_holds(id) ON DELETE CASCADE,
    tenant_id         UUID         NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- pending | approved | rejected | withdrawn | superseded
    status            VARCHAR(12)  NOT NULL DEFAULT 'pending',
    -- the hold id while pending, NULL once decided: one open request per hold
    pending_key       VARCHAR(64)  NULL,
    reason            TEXT         NOT NULL,
    requested_by      VARCHAR(255) NOT NULL,
    requested_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    approver_group_id UUID         NULL,
    decided_by        VARCHAR(255) NULL,
    decided_at        TIMESTAMPTZ  NULL,
    decision_note     TEXT         NULL,

    CONSTRAINT ck_lh_release_requests_status
        CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn', 'superseded'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_lh_release_requests_pending
    ON legal_hold_release_requests (pending_key);
CREATE INDEX IF NOT EXISTS ix_lh_release_requests_hold
    ON legal_hold_release_requests (hold_id, status);
