"""HxDBMigrate P4 — batch data migration: source table rows → Velaris cases.

Reads a bounded, paged window of source rows (read-only) and creates `case_instances` of a
target case-type with the row mapped into case `data`. A **lean insert** — it does NOT fire
outbound rules / webhooks (a historical import must never trigger business automation).

PII handling (`pii_mode`), honouring the P2 compliance scan classified from the rows read:
  * ``safe`` (default) — columns whose recommended action is **tokenize** (cards, SSNs) are
    NOT copied. Ordinary customer PII (email/name/phone) IS migrated — it is the data the
    tenant is deliberately moving.
  * ``exclude_all`` — drop every compliance-flagged column.
  * ``as_is`` — copy everything verbatim (admin explicitly accepts moving raw PII).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db import repository as repo
from case_service.db.introspection import get_introspector
from case_service.hxdbmigrate import compliance, report as _report, semantic

MAX_LIMIT = 1000    # hard cap on rows migrated per run (bounded — no unbounded load)

_PII_MODES = ("safe", "exclude_all", "as_is")


def pii_modes() -> list[str]:
    return list(_PII_MODES)


def _exclusions(columns: list[str], rows: list[dict[str, Any]], pii_mode: str) -> set[str]:
    """Columns to drop, from the compliance scan classified over the sampled rows."""
    if pii_mode == "as_is":
        return set()
    sem = {c: semantic.classify_column(c, [r.get(c) for r in rows]) for c in columns}
    findings = compliance.scan({"_": sem})["findings"]
    if pii_mode == "exclude_all":
        return {f["column"] for f in findings}
    return {f["column"] for f in findings if f["recommended_action"] == "tokenize"}  # safe


async def migrate_table(
    src_session: AsyncSession,
    target_session: AsyncSession,
    *,
    table: str,
    case_type_id,
    case_type_version: str,
    tenant_id,
    created_by: str | None,
    limit: int = 100,
    offset: int = 0,
    pii_mode: str = "safe",
    dry_run: bool = False,
    source_id=None,
) -> dict[str, Any]:
    """Migrate up to ``limit`` rows (from ``offset``) of ``table`` into cases.

    P5: when ``source_id`` is given and the table has a single-column PK, every
    migrated row is recorded in ``hxdbmigrate_row_links`` and rows already linked
    are SKIPPED — batch migrate becomes idempotent and feeds the sync/cutover
    identity spine. Tables without a usable PK still migrate (no links, noted).
    """
    if pii_mode not in _PII_MODES:
        raise ValueError(f"Unsupported pii_mode {pii_mode!r}. Allowed: {list(_PII_MODES)}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    insp = get_introspector(src_session)
    if not await insp.table_exists(src_session, table):
        raise ValueError(f"Table {table!r} not found in source")
    columns = [c["column_name"] for c in await insp.columns(src_session, table)]

    pk: str | None = None
    if source_id is not None:
        from case_service.hxdbmigrate import sync as sync_mod
        try:
            pk = await sync_mod.pk_column(src_session, table)
        except sync_mod.SyncError:
            pk = None                       # migrate still works; links skipped

    # ORDER BY 1 (first column, usually the PK) for stable paging across runs.
    q = f"SELECT * FROM {insp.quote_ident(table)} ORDER BY 1 LIMIT {limit} OFFSET {offset}"
    rows = [dict(r) for r in (await src_session.execute(text(q))).mappings().all()]

    excluded = _exclusions(columns, rows, pii_mode)

    already_linked: set[str] = set()
    if pk is not None and rows and not dry_run:
        from sqlalchemy import select
        from case_service.db.models import HxDBMigrateRowLinkModel
        already_linked = {l for (l,) in (await target_session.execute(
            select(HxDBMigrateRowLinkModel.source_pk).where(
                HxDBMigrateRowLinkModel.source_id == source_id,
                HxDBMigrateRowLinkModel.table_name == table,
                HxDBMigrateRowLinkModel.source_pk.in_(
                    [str(r[pk]) for r in rows]),
            ))).all()}

    migrated = skipped = 0
    preview: list[dict[str, Any]] = []
    for r in rows:
        data = _report._jsonable({k: v for k, v in r.items() if k not in excluded})
        if dry_run:
            if len(preview) < 5:
                preview.append(data)
            continue
        if pk is not None and str(r[pk]) in already_linked:
            skipped += 1                    # idempotent: this row is already a case
            continue
        case = await repo.create_case_instance(target_session, data={
            "case_type_id": case_type_id,
            "case_type_version": case_type_version,
            "status": "new",
            "priority": "medium",
            "data": data,
            "created_by": created_by,
            "tenant_id": tenant_id,
        })
        if pk is not None:
            from case_service.hxdbmigrate import sync as sync_mod
            from case_service.db.models import HxDBMigrateRowLinkModel
            target_session.add(HxDBMigrateRowLinkModel(
                source_id=source_id, tenant_id=tenant_id, table_name=table,
                source_pk=str(r[pk]), case_id=case.id, case_type_id=case_type_id,
                row_checksum=sync_mod.row_checksum(data),
            ))
        migrated += 1

    return {
        "rows_read": len(rows),
        "rows_migrated": 0 if dry_run else migrated,
        "rows_skipped_already_linked": skipped,
        "linked": pk is not None,
        "pk_column": pk,
        "excluded_columns": sorted(excluded),
        "pii_mode": pii_mode,
        "dry_run": dry_run,
        "preview": preview,
    }
