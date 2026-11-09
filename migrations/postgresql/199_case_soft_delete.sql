-- 199 — a case can be deleted, and deletion is SOFT.
--
-- Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md §7, answered 2026-09-08.
--
-- WHY THERE WAS NO DELETE AT ALL. Until now the only `DELETE` under `/cases`
-- was `/{id}/shares`. That was not an oversight — a hard delete of a case
-- conflicts with two things this platform takes seriously. The audit chain is
-- hash-linked and an auditor asking "show me what happened on 3 March" must not
-- be told the case never existed; and GDPR erasure is already implemented, as
-- `anonymize`, which scrubs the subject and KEEPS the case. A hard delete would
-- be a second, weaker erasure that also destroyed the evidence the first one
-- deliberately preserves.
--
-- So: soft only. The row stays, the history stays, the case disappears from
-- every list and every read. Erasure keeps owning the hard case.
--
-- ADDITIVE AND RE-RUNNABLE. Three nullable-or-defaulted columns on an existing
-- table. Nothing is rewritten, no existing column changes type, and a case that
-- predates this migration reads as `is_deleted = false`, which is what it is.
-- An install that never uses the action never sees a difference.
--
-- NOT A STATUS. `deleted` is deliberately NOT added to `case_statuses`. A status
-- says where a case is IN its process; deletion says the case should not be
-- looked at. Modelling it as a status would mean every status-set query, every
-- terminal check and every customer-facing rail had to learn to special-case
-- one name — and a tenant could then delete a case by editing a dropdown.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE case_instances ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE case_instances ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;
ALTER TABLE case_instances ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255) NULL;

-- Every list query gains `is_deleted = false`, so the index carries it.
-- Partial, because the deleted rows are the ones nobody reads.
CREATE INDEX IF NOT EXISTS idx_case_instances_not_deleted
    ON case_instances (tenant_id, status)
    WHERE is_deleted = FALSE;
