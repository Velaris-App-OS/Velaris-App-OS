"""Audit log hash chain — out-of-band tamper detection.

Design:
  - Audit writes are NOT modified. Existing append_audit_entry stays fast.
  - A separate sealer (sync or scheduled) walks new audit rows and writes
    one hash-chain row per audit row.
  - Each chain row stores prev_hash + content_hash where:
       content_hash = sha256(prev_hash || canonical(audit_row))
  - Tampering with audit OR chain is detectable: rebuild content_hash from
    audit data, compare to stored content_hash. Any divergence flags a break.

Performance: O(n) one-pass scan for both seal and verify. Sealer state
(sequence + last_hash) read once per call from the most recent chain row.
"""
from __future__ import annotations
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

# Postgres advisory lock key for seal serialization — b"HELIXCHA" as big-endian int64.
# Held for the duration of the sealing transaction; auto-released on commit/rollback.
_SEAL_LOCK_KEY = int.from_bytes(b"HELIXCHA", "big")

from case_service.db.models import AuditChainModel, CaseAuditLogModel

log = logging.getLogger(__name__)
GENESIS_HASH = "0" * 64


def compute_row_hash(audit_row: CaseAuditLogModel, prev_hash: str) -> str:
    """Canonical-JSON hash of an audit row chained off prev_hash."""
    canonical = json.dumps({
        "audit_log_id": str(audit_row.id),
        "case_id": str(audit_row.case_id) if audit_row.case_id else None,
        "action": audit_row.action,
        "actor_id": audit_row.actor_id,
        "actor_type": getattr(audit_row, "actor_type", None),
        "details": audit_row.details or {},
        "timestamp": audit_row.timestamp.isoformat() if audit_row.timestamp else None,
        "prev_hash": prev_hash,
    }, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _last_chain_row(session: AsyncSession) -> AuditChainModel | None:
    q = select(AuditChainModel).order_by(AuditChainModel.sequence.desc()).limit(1)
    res = await session.execute(q)
    return res.scalar_one_or_none()


async def _unsealed_audit_rows(
    session: AsyncSession, after_id: uuid.UUID | None,
) -> list[CaseAuditLogModel]:
    """Audit rows not yet sealed.

    Strategy: rely on monotonic timestamp + id ordering. We seal in
    timestamp order; rows with timestamp > last_sealed_timestamp OR
    same timestamp + id > last_sealed_id are unsealed.

    Simpler: NOT IN (SELECT audit_log_id FROM chain). We use a LEFT JOIN
    via subquery for portability across SQLite + Postgres.
    """
    sealed_ids_subq = select(AuditChainModel.audit_log_id).subquery()
    q = (
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.id.notin_(select(sealed_ids_subq.c.audit_log_id)))
        .order_by(CaseAuditLogModel.timestamp.asc(), CaseAuditLogModel.id.asc())
    )
    res = await session.execute(q)
    return list(res.scalars().all())


async def seal_new_entries(session: AsyncSession, max_rows: int = 10000) -> dict:
    """Seal up to max_rows new audit entries. Returns count + new tip hash.

    Acquires a Postgres transaction-level advisory lock before reading the
    chain tip, so concurrent seal calls queue up rather than racing to insert
    duplicate sequence numbers.
    """
    try:
        await session.execute(text(f"SELECT pg_advisory_xact_lock({_SEAL_LOCK_KEY})"))
    except Exception:
        pass  # Non-Postgres (e.g. SQLite in tests) — unique constraint is the fallback guard
    last = await _last_chain_row(session)
    last_seq = last.sequence if last else 0
    last_hash = last.content_hash if last else GENESIS_HASH

    pending = await _unsealed_audit_rows(session, last.audit_log_id if last else None)
    pending = pending[:max_rows]

    sealed = 0
    for audit_row in pending:
        h = compute_row_hash(audit_row, last_hash)
        chain = AuditChainModel(
            id=uuid.uuid4(),
            sequence=last_seq + 1,
            audit_log_id=audit_row.id,
            prev_hash=last_hash,
            content_hash=h,
        )
        session.add(chain)
        last_hash = h
        last_seq += 1
        sealed += 1

    if sealed:
        await session.flush()
        log.info("sealed %d audit entries (tip=%d)", sealed, last_seq)

    return {
        "sealed": sealed,
        "tip_sequence": last_seq,
        "tip_hash": last_hash,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }


async def verify_chain(session: AsyncSession, limit: int | None = None) -> dict:
    """Walk the chain in order. Recompute hashes. Report any breaks.

    Returns: {verified, chain_length, breaks: [...], tip_sequence, tip_hash}
    """
    q = select(AuditChainModel).order_by(AuditChainModel.sequence.asc())
    if limit:
        q = q.limit(limit)
    res = await session.execute(q)
    chain = list(res.scalars().all())

    breaks: list[dict] = []
    expected_prev = GENESIS_HASH

    for row in chain:
        # 1. Linkage check
        if row.prev_hash != expected_prev:
            breaks.append({
                "sequence": row.sequence,
                "type": "broken_link",
                "expected_prev": expected_prev,
                "actual_prev": row.prev_hash,
            })
            # Continue with row.prev_hash as new baseline so downstream
            # rows aren't all reported as broken too:
            expected_prev = row.content_hash
            continue

        # 2. Content check
        audit_row = await session.get(CaseAuditLogModel, row.audit_log_id)
        if audit_row is None:
            breaks.append({
                "sequence": row.sequence,
                "type": "missing_audit_row",
                "audit_log_id": str(row.audit_log_id),
            })
            expected_prev = row.content_hash
            continue

        recomputed = compute_row_hash(audit_row, row.prev_hash)
        if recomputed != row.content_hash:
            breaks.append({
                "sequence": row.sequence,
                "type": "content_tampered",
                "audit_log_id": str(row.audit_log_id),
                "stored_hash": row.content_hash,
                "recomputed_hash": recomputed,
            })

        expected_prev = row.content_hash

    return {
        "verified": len(breaks) == 0,
        "chain_length": len(chain),
        "breaks": breaks,
        "tip_sequence": chain[-1].sequence if chain else 0,
        "tip_hash": chain[-1].content_hash if chain else GENESIS_HASH,
    }


async def chain_status(session: AsyncSession) -> dict:
    """Lightweight summary — no verification scan."""
    q_audit = select(func.count()).select_from(CaseAuditLogModel)
    q_chain = select(func.count()).select_from(AuditChainModel)
    audit_count = (await session.execute(q_audit)).scalar_one()
    chain_count = (await session.execute(q_chain)).scalar_one()
    last = await _last_chain_row(session)
    return {
        "audit_rows": int(audit_count),
        "sealed_rows": int(chain_count),
        "unsealed_rows": int(audit_count - chain_count),
        "tip_sequence": last.sequence if last else 0,
        "tip_hash": last.content_hash if last else GENESIS_HASH,
        "last_sealed_at": last.sealed_at.isoformat() if last and last.sealed_at else None,
    }
