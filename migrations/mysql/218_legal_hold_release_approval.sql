-- 218 — legal hold release: optional second-person approval (2026-10-14).
-- MySQL / MariaDB twin of migrations/postgresql/218; the reasoning is there.
-- Additive: two new tables. A tenant with no settings row releases as before.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS legal_hold_settings (
    tenant_id         CHAR(36)     PRIMARY KEY,
    approver_group_id CHAR(36)     NULL,
    updated_by        VARCHAR(255) NOT NULL,
    updated_at        DATETIME(6)  NOT NULL,

    CONSTRAINT fk_legal_hold_settings_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS legal_hold_release_requests (
    id                CHAR(36)     PRIMARY KEY,
    hold_id           CHAR(36)     NOT NULL,
    tenant_id         CHAR(36)     NULL,
    status            VARCHAR(12)  NOT NULL DEFAULT 'pending',
    pending_key       VARCHAR(64)  NULL,
    reason            TEXT         NOT NULL,
    requested_by      VARCHAR(255) NOT NULL,
    requested_at      DATETIME(6)  NOT NULL,
    approver_group_id CHAR(36)     NULL,
    decided_by        VARCHAR(255) NULL,
    decided_at        DATETIME(6)  NULL,
    decision_note     TEXT         NULL,

    UNIQUE KEY ux_lh_release_requests_pending (pending_key),
    KEY ix_lh_release_requests_hold (hold_id, status),
    CONSTRAINT fk_lh_release_requests_hold FOREIGN KEY (hold_id)
        REFERENCES legal_holds(id) ON DELETE CASCADE,
    CONSTRAINT fk_lh_release_requests_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE CASCADE,
    CONSTRAINT ck_lh_release_requests_status
        CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn', 'superseded'))
);
