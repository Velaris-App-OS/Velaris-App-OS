"""HxDBMigrate P5 — continuous sync: the polling Migration Twin.

Keeps migrated cases in step with a LIVE source table until cutover, with zero
new infrastructure (no Debezium/Kafka — signed-off decision 2026-07-05):

  * identity = ``hxdbmigrate_row_links`` (source row PK → case id), unique per
    (source, table, pk) — sync is idempotent by construction;
  * change detection = SHA-256 row checksum over the PII-filtered payload —
    engine-agnostic, no reliance on updated_at conventions;
  * keyset pagination by primary key (bounded pages, bounded pass) — the source
    is only ever read, in stable order, never locked or written;
  * the SAME PII exclusion gate as P4 batch migration (a column that must not
    migrate must not sync in either);
  * lean case writes — updates never fire rules/webhooks (a sync pass must not
    trigger business automation), exactly like the P4 import posture.

A single-column primary key is REQUIRED for sync (the link identity). Tables
without one still work with P4's one-shot batch migrate; sync refuses honestly.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db import repository as repo
from case_service.db.introspection import get_introspector
from case_service.db.models import CaseInstanceModel, HxDBMigrateRowLinkModel
from case_service.hxdbmigrate import report as _report
from case_service.hxdbmigrate.migrate import _exclusions

PAGE_SIZE = 500          # rows fetched per source page
MAX_PAGES_PER_CALL = 10  # one sync call touches at most PAGE_SIZE * this rows


class SyncError(Exception):
    """Sync cannot run for a reason the caller should state honestly."""


# Engine-aware PK lookups that need only SELECT privilege. MariaDB hides
# information_schema.table_constraints rows from SELECT-only users (verified
# live), while information_schema.columns.column_key stays visible — so the
# MySQL-family path reads column_key='PRI' instead of the constraints join.
_PK_QUERY_MYSQL = """
SELECT column_name FROM information_schema.columns
WHERE table_schema = DATABASE() AND table_name = :table_name
  AND column_key = 'PRI'
ORDER BY ordinal_position
"""
_PK_QUERY_PG = """
SELECT a.attname AS column_name
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
WHERE i.indisprimary AND c.relname = :table_name
ORDER BY a.attnum
"""


async def pk_column(src_session: AsyncSession, table: str) -> str:
    """The table's single-column primary key — sync's identity requirement.

    Composite or missing PKs are refused (batch migrate still works for those
    tables).
    """
    dialect = src_session.get_bind().dialect.name
    q = _PK_QUERY_PG if dialect == "postgresql" else _PK_QUERY_MYSQL
    rows = (await src_session.execute(
        text(q), {"table_name": table})).scalars().all()
    cols = list(dict.fromkeys(rows))
    if not cols:
        raise SyncError(f"Table {table!r} has no primary key — continuous sync "
                        f"needs one (batch migrate still works)")
    if len(cols) > 1:
        raise SyncError(f"Table {table!r} has a composite primary key "
                        f"({', '.join(cols)}) — continuous sync needs a "
                        f"single-column key")
    return cols[0]


def row_checksum(data: dict[str, Any]) -> str:
    """Stable checksum of the PII-filtered, JSON-safe payload."""
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


_INT_TYPES = ("int", "serial", "number", "numeric", "decimal")


def coerce_pk_cursor(value: str | None, pk_data_type: str) -> Any:
    """The API cursor is a string; PostgreSQL will NOT coerce text against an
    integer PK (``integer > text`` has no operator — MySQL silently casts), so
    an integer-typed key gets its cursor coerced back before binding."""
    if value is None:
        return None
    if any(t in (pk_data_type or "").lower() for t in _INT_TYPES):
        try:
            return int(value)
        except ValueError:
            raise SyncError(f"Cursor {value!r} is not valid for the integer "
                            f"primary key")
    return value


async def sync_table(
    src_session: AsyncSession,
    target_session: AsyncSession,
    *,
    source_id,
    table: str,
    case_type_id,
    case_type_version: str,
    tenant_id,
    created_by: str | None,
    pii_mode: str = "safe",
    after_pk: str | None = None,
) -> dict[str, Any]:
    """One bounded sync pass over ``table``: new rows → cases, changed rows →
    case-data updates, unchanged rows skipped. Returns a cursor when the table
    is larger than one pass."""
    insp = get_introspector(src_session)
    if not await insp.table_exists(src_session, table):
        raise SyncError(f"Table {table!r} not found in source")
    pk = await pk_column(src_session, table)
    column_meta = await insp.columns(src_session, table)
    columns = [c["column_name"] for c in column_meta]
    pk_type = next((str(c.get("data_type") or "") for c in column_meta
                    if c["column_name"] == pk), "")

    created = updated = unchanged = rows_read = 0
    # cursor stays NATIVE-typed in the loop (a stringified cursor breaks
    # PostgreSQL integer keys); only the response stringifies it
    cursor: Any = coerce_pk_cursor(after_pk, pk_type)
    now = datetime.now(timezone.utc)

    for _ in range(MAX_PAGES_PER_CALL):
        qi_table, qi_pk = insp.quote_ident(table), insp.quote_ident(pk)
        if cursor is None:
            q = f"SELECT * FROM {qi_table} ORDER BY {qi_pk} LIMIT {PAGE_SIZE}"
            rows = (await src_session.execute(text(q))).mappings().all()
        else:
            q = (f"SELECT * FROM {qi_table} WHERE {qi_pk} > :after "
                 f"ORDER BY {qi_pk} LIMIT {PAGE_SIZE}")
            rows = (await src_session.execute(
                text(q), {"after": cursor})).mappings().all()
        rows = [dict(r) for r in rows]
        if not rows:
            cursor = None
            break
        rows_read += len(rows)
        cursor = rows[-1][pk]

        excluded = _exclusions(columns, rows, pii_mode)
        pks = [str(r[pk]) for r in rows]
        links = {l.source_pk: l for l in (await target_session.execute(
            select(HxDBMigrateRowLinkModel).where(
                HxDBMigrateRowLinkModel.source_id == source_id,
                HxDBMigrateRowLinkModel.table_name == table,
                HxDBMigrateRowLinkModel.source_pk.in_(pks),
            ))).scalars().all()}

        for r in rows:
            data = _report._jsonable({k: v for k, v in r.items()
                                      if k not in excluded})
            checksum = row_checksum(data)
            link = links.get(str(r[pk]))
            if link is None:
                case = await repo.create_case_instance(target_session, data={
                    "case_type_id": case_type_id,
                    "case_type_version": case_type_version,
                    "status": "new", "priority": "medium",
                    "data": data, "created_by": created_by,
                    "tenant_id": tenant_id,
                })
                target_session.add(HxDBMigrateRowLinkModel(
                    source_id=source_id, tenant_id=tenant_id, table_name=table,
                    source_pk=str(r[pk]), case_id=case.id,
                    case_type_id=case_type_id, row_checksum=checksum,
                    last_synced_at=now,
                ))
                created += 1
            elif link.row_checksum != checksum:
                case = await target_session.get(CaseInstanceModel, link.case_id)
                if case is None:      # case deleted in Velaris → recreate + relink
                    case = await repo.create_case_instance(target_session, data={
                        "case_type_id": case_type_id,
                        "case_type_version": case_type_version,
                        "status": "new", "priority": "medium",
                        "data": data, "created_by": created_by,
                        "tenant_id": tenant_id,
                    })
                    link.case_id = case.id
                else:
                    case.data = data      # lean update — no rules/webhooks fired
                link.row_checksum = checksum
                link.last_synced_at = now
                updated += 1
            else:
                unchanged += 1

        if len(rows) < PAGE_SIZE:
            cursor = None
            break

    return {
        "rows_read": rows_read,
        "cases_created": created,
        "cases_updated": updated,
        "rows_unchanged": unchanged,
        "pk_column": pk,
        "pii_mode": pii_mode,
        "done": cursor is None,
        "next_after_pk": str(cursor) if cursor is not None else None,
    }


async def sync_status(
    src_session: AsyncSession,
    target_session: AsyncSession,
    *,
    source_id,
) -> dict[str, Any]:
    """Per-table sync coverage + a deterministic Migration Health Score (0-100).

    Coverage = linked rows / live source rows. The score is the mean table
    coverage minus a staleness penalty — honest and reproducible, no ML.
    """
    from sqlalchemy import func

    insp = get_introspector(src_session)
    grouped = (await target_session.execute(
        select(HxDBMigrateRowLinkModel.table_name,
               func.count(HxDBMigrateRowLinkModel.id),
               func.max(HxDBMigrateRowLinkModel.last_synced_at))
        .where(HxDBMigrateRowLinkModel.source_id == source_id)
        .group_by(HxDBMigrateRowLinkModel.table_name)
    )).all()

    tables: list[dict[str, Any]] = []
    coverages: list[float] = []
    now = datetime.now(timezone.utc)
    for table_name, linked, last_synced in grouped:
        source_rows: int | None = None
        if await insp.table_exists(src_session, table_name):
            source_rows = (await src_session.execute(text(
                f"SELECT COUNT(*) FROM {insp.quote_ident(table_name)}"
            ))).scalar()
        coverage = (min(1.0, linked / source_rows)
                    if source_rows else (1.0 if source_rows == 0 else 0.0))
        coverages.append(coverage)
        if last_synced is not None and last_synced.tzinfo is None:
            last_synced = last_synced.replace(tzinfo=timezone.utc)
        tables.append({
            "table": table_name,
            "linked_rows": linked,
            "source_rows": source_rows,
            "coverage_pct": round(coverage * 100, 1),
            "last_synced_at": last_synced.isoformat() if last_synced else None,
            "stale_hours": (round((now - last_synced).total_seconds() / 3600, 1)
                            if last_synced else None),
        })

    if not tables:
        return {"tables": [], "health_score": None,
                "hint": "No rows migrated yet — run a migration first."}

    mean_coverage = sum(coverages) / len(coverages)
    worst_stale = max((t["stale_hours"] or 0) for t in tables)
    staleness_penalty = min(20.0, worst_stale * 0.5)   # −0.5/h, capped at −20
    score = max(0, round(mean_coverage * 100 - staleness_penalty))
    return {"tables": tables, "health_score": score}
