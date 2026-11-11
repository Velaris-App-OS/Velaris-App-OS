"""Permanent erasure of cases (2026-11-10, the website's leads).

The case delete action is SOFT on purpose (the row and its history stay); this
is the other thing — every trace of a case, gone, for a person who withdrew
consent or deleted their account. Used by the website's lead erasure
(internal_api/demo_erase.py), and written so it can serve a general GDPR delete.

What goes, in one transaction, for each case NOT under a legal hold:

  1. its audit rows — through `audit_chain.prune_audit_rows`, which seals a
     prune record into the chain in the same transaction (mig 216), so the
     chain still verifies and says rows were removed and why;
  2. every row of every table with a `case_id` column — found from the schema
     itself, not a hand list, so a table added later is covered without anyone
     remembering it — and the rows of any table that references THOSE rows
     (two levels), children first;
  3. any stored file such a row names (`storage_key` and friends), from the
     storage backend, before the row goes;
  4. the case rows themselves.

A held case is left whole and reported. Nothing personal is kept about what
was erased: callers record a fingerprint (sha256) of the subject, never the
subject.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Sequence

from sqlalchemy import Table, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

CASE_COLUMNS = ("case_id", "case_instance_id")
SKIP = {"case_instances", "case_audit_log", "case_audit_log_chain_prunes", "legal_holds"}
STORAGE_COLUMNS = ("storage_key", "storage_key_json", "storage_key_pdf", "staged_storage_key")


def _tables() -> list[Table]:
    from velaris_shared.db.models import Base
    return list(Base.metadata.sorted_tables)


def dependents() -> list[tuple[Table, Any]]:
    """(table, column) for every table holding a case reference."""
    out = []
    for t in _tables():
        if t.name in SKIP:
            continue
        for name in CASE_COLUMNS:
            if name in t.c:
                out.append((t, t.c[name]))
                break
    return out


def _children_of(parent: Table) -> list[tuple[Table, Any, Any]]:
    """(child table, its FK column, the parent column it points at)."""
    out = []
    for t in _tables():
        if t is parent:
            continue
        for fk in t.foreign_keys:
            if fk.column.table is parent:
                out.append((t, fk.parent, fk.column))
    return out


async def _drop_files(session: AsyncSession, table: Table, where) -> int:
    cols = [table.c[c] for c in STORAGE_COLUMNS if c in table.c]
    if not cols:
        return 0
    keys = set()
    for row in (await session.execute(select(*cols).where(where))).all():
        keys.update(k for k in row if k)
    if not keys:
        return 0
    from velaris_shared.storage.factory import get_storage_backend
    backend = get_storage_backend()
    n = 0
    for k in keys:
        try:
            await backend.delete(k)
            n += 1
        except Exception as exc:                 # noqa: BLE001 — a missing file is already gone
            log.warning("erasure: could not delete stored file %s: %s", k, exc)
    return n


async def _delete_where(session: AsyncSession, table: Table, where, counts: dict, depth: int = 0) -> None:
    """Delete `table` rows matching `where`, their children first (2 levels)."""
    if depth < 2:
        for child, fk_col, parent_col in _children_of(table):
            if child.name in SKIP:
                continue
            sub = select(parent_col).where(where)
            await _delete_where(session, child, fk_col.in_(sub), counts, depth + 1)
    counts["files"] = counts.get("files", 0) + await _drop_files(session, table, where)
    n = (await session.execute(delete(table).where(where).execution_options(
        synchronize_session=False))).rowcount or 0
    if n:
        counts[table.name] = counts.get(table.name, 0) + int(n)


async def erase_cases(session: AsyncSession, case_ids: Sequence, *, actor: str,
                      reason: str = "erasure") -> dict:
    """Erase these cases for good. Returns {"erased": [...], "held": [...],
    "rows": {table: n}}. The caller commits."""
    from velaris_shared.db.models import CaseAuditLogModel, CaseInstanceModel
    from case_service.compliance import audit_chain
    from case_service.legal_hold import service as legal_hold

    ids = [uuid.UUID(str(i)) for i in case_ids]
    if not ids:
        return {"erased": [], "held": [], "rows": {}}
    free_where = await legal_hold.not_held(session, CaseInstanceModel.id)
    stmt = select(CaseInstanceModel.id).where(CaseInstanceModel.id.in_(ids))
    if free_where is not None:
        stmt = stmt.where(free_where)
    free = list((await session.execute(stmt)).scalars())
    held = [str(i) for i in ids if i not in set(free)]
    counts: dict[str, int] = {}
    if free:
        counts["case_audit_log"] = await audit_chain.prune_audit_rows(
            session, (CaseAuditLogModel.case_id.in_(free),), reason=reason, actor=actor)
        for table, col in dependents():
            await _delete_where(session, table, col.in_(free), counts)
        cases = CaseInstanceModel.__table__
        # child cases of an erased case lose their parent link, not their life
        if "parent_case_id" in cases.c:
            from sqlalchemy import update
            await session.execute(update(cases).where(cases.c.parent_case_id.in_(free))
                                  .values(parent_case_id=None).execution_options(synchronize_session=False))
        await _delete_where(session, cases, cases.c.id.in_(free), counts)
    log.info("erasure by %s: %d case(s) erased, %d held; rows %s", actor, len(free), len(held), counts)
    return {"erased": [str(i) for i in free], "held": held, "rows": counts}


def _blocking_tables() -> list[tuple[Table, Any]]:
    """Tables whose foreign key to `tenants` does NOT cascade (found from the
    schema): their rows must go before the tenant can (2026-11-10)."""
    out = []
    for t in _tables():
        if t.name == "tenants":
            continue
        for fk in t.foreign_keys:
            if fk.column.table.name == "tenants" and (fk.ondelete or "").upper() not in ("CASCADE", "SET NULL"):
                out.append((t, fk.parent))
    return out


async def erase_tenant_data(session: AsyncSession, tenant_uuid, *, actor: str,
                            reason: str = "tenant-deleted") -> dict:
    """Everything a tenant holds that would block deleting the tenant row: its
    cases erased for good (as erase_cases), then the rows of every table whose
    tenant foreign key does not cascade, children first. Held cases make it
    refuse (ValueError) — the caller has already checked legal holds. The
    caller deletes the tenant and commits."""
    from velaris_shared.db.models import CaseInstanceModel
    tid = uuid.UUID(str(tenant_uuid))
    ids = list((await session.execute(select(CaseInstanceModel.id).where(
        CaseInstanceModel.tenant_id == tid))).scalars())
    out = await erase_cases(session, ids, actor=actor, reason=reason)
    if out["held"]:
        raise ValueError(f"{len(out['held'])} case(s) under a legal hold")
    counts = dict(out["rows"])
    order = {t.name: i for i, t in enumerate(_tables())}
    for table, col in sorted(_blocking_tables(), key=lambda tc: -order.get(tc[0].name, 0)):
        if table.name == "case_instances":
            continue
        await _delete_where(session, table, col == tid, counts)
    log.info("tenant %s data erased by %s: %s", tid, actor, counts)
    return {"cases": len(out["erased"]), "rows": counts}
