"""F-01 — sealing the AI ledger, so it is evidence rather than rows.

The shape is `hxarbiter/chain.py`, deliberately and almost line for line:
batched windows, `content_hash = sha256(prev_hash || canonical(batch))`,
contiguous by construction so a gap means rows were removed, and a tombstone
pair so retention and tamper-evidence can coexist.

Two things are different, and both are the point of this file:

* **One chain covers records AND verdicts.** A verdict is part of the evidence,
  not a footnote to it. Sealing them separately would let a human's override be
  rewritten while the decision it overrode still verified — which is precisely
  the row an Article 22 challenge is about.
* **The tombstone is here from the first commit.** Migration 180 shipped a
  retention horizon for the arbiter log that destroyed its chain the first time
  an operator ran it, because deleting the rows a sealed batch covers makes the
  seal unverifiable forever. That was found by running a purge, not by reading
  the code. It is not being learned twice.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    AIDecisionRecordModel,
    AIDecisionVerdictModel,
    AILedgerChainModel,
)

log = logging.getLogger(__name__)

BATCH_SECONDS = 60
#: A row written inside an open transaction is not visible yet; sealing right
#: up to `now` would leave it unsealed forever, since the sealer only ever
#: looks forward.
SETTLE_SECONDS = 5
GENESIS = "0" * 64
LOOP_INTERVAL_SECONDS = 60
#: A day of one-minute windows. Caps the damage from a single row bearing a
#: far-past timestamp — a restored backup, a clock that jumped, a badly seeded
#: fixture — which would otherwise spin out millions of empty batches at once.
MAX_BATCHES_PER_RUN = 1440


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_batch_hash(
    prev_hash: str,
    records: list[AIDecisionRecordModel],
    verdicts: list[AIDecisionVerdictModel],
    window_start: datetime,
    window_end: datetime,
) -> str:
    """`sha256(prev_hash || canonical(batch))`.

    Only the fields that make a record what it is are hashed, sorted by id so
    the digest cannot depend on the order rows came back in. `chain_sequence` is
    excluded: this very process is about to write it, and hashing it would make
    the seal unverifiable a moment after it was made.

    **Callers must pass rows READ BACK FROM THE DATABASE**, never rows still in
    a session's identity map — and `seal_once`/`verify_chain` enforce that with
    `populate_existing=True` rather than trusting themselves to remember.

    That is not tidiness. A session created with `expire_on_commit=False`
    (which is every session in the test harness, and a perfectly ordinary
    production choice) keeps returning the objects it already holds, so a row
    altered by anything other than that session verifies against the values the
    session still remembers. The chain then reports "verified" over tampered
    data — a false pass, which is the single worst outcome available to this
    function. It was caught by a test that edited a sealed row and watched
    verification succeed.

    Timestamps are a second reason: they are hashed via `isoformat()`, and an
    aware value renders with `+00:00` where the same instant read back from a
    naive column renders without it. Seal one way and verify the other and the
    digests differ for a reason that has nothing to do with tampering.
    """
    payload = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "records": [
            {
                "id": str(r.id),
                "tenant_id": str(r.tenant_id),
                "agent_id": str(r.agent_id) if r.agent_id else None,
                "agent_slug": r.agent_slug,
                "agent_kind": r.agent_kind,
                "model_id": r.model_id,
                "provider_class": r.provider_class,
                "purpose": r.purpose,
                "case_id": str(r.case_id) if r.case_id else None,
                "step_id": r.step_id,
                "input_sha256": r.input_sha256,
                "input_data_classes": r.input_data_classes,
                "output_sha256": r.output_sha256,
                # The SEALED text is hashed too. It is ciphertext, so this adds
                # no plaintext to the digest — and without it, an operator who
                # can reach the database could swap the stored transcript for
                # another and the chain would still verify.
                "prompt_sealed": r.prompt_sealed,
                "output_sealed": r.output_sealed,
                "confidence": str(r.confidence) if r.confidence is not None else None,
                "latency_ms": str(r.latency_ms) if r.latency_ms is not None else None,
                "arbiter_decision_id": (str(r.arbiter_decision_id)
                                        if r.arbiter_decision_id else None),
                "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
            }
            for r in sorted(records, key=lambda x: str(x.id))
        ],
        "verdicts": [
            {
                "id": str(v.id),
                "record_id": str(v.record_id),
                "tenant_id": str(v.tenant_id),
                "verdict": v.verdict,
                "diff_sha256": v.diff_sha256,
                "diff_sealed": v.diff_sealed,
                "reason": v.reason,
                "decided_by": v.decided_by,
                "decided_at": v.decided_at.isoformat() if v.decided_at else None,
            }
            for v in sorted(verdicts, key=lambda x: str(x.id))
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


async def _tip(session: AsyncSession) -> tuple[int, str, datetime | None]:
    """(sequence, hash, window_end) of the newest sealed batch."""
    row = (await session.execute(
        select(AILedgerChainModel)
        .order_by(AILedgerChainModel.sequence.desc()).limit(1)
    )).scalars().first()
    if row is None:
        return 0, GENESIS, None
    return row.sequence, row.content_hash, row.window_end


async def _oldest_unsealed(session: AsyncSession) -> datetime | None:
    """The earliest moment either table has an unsealed row.

    Both tables, because the window covers both. Taking the records' minimum
    alone would start the chain after a verdict that arrived first — which
    cannot be sealed later, since the sealer only walks forward.
    """
    r = (await session.execute(
        select(func.min(AIDecisionRecordModel.recorded_at))
        .where(AIDecisionRecordModel.chain_sequence.is_(None))
    )).scalar_one_or_none()
    v = (await session.execute(
        select(func.min(AIDecisionVerdictModel.decided_at))
        .where(AIDecisionVerdictModel.chain_sequence.is_(None))
    )).scalar_one_or_none()
    candidates = [t for t in (r, v) if t is not None]
    if not candidates:
        return None
    oldest = min(candidates)
    return oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)


async def seal_once(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Seal every complete batch that is ready. Returns how many were sealed.

    An **empty window is still sealed**. Skipping empty ones would leave gaps
    indistinguishable from deleted rows, and the whole value of contiguous
    windows is that a gap means something.
    """
    now = now or _utcnow()
    horizon = now - timedelta(seconds=SETTLE_SECONDS)

    sequence, prev_hash, last_end = await _tip(session)
    if last_end is None:
        window_start = await _oldest_unsealed(session)
        if window_start is None:
            return 0                       # nothing has ever been decided
    else:
        window_start = (last_end if last_end.tzinfo
                        else last_end.replace(tzinfo=timezone.utc))

    sealed = 0
    while sealed < MAX_BATCHES_PER_RUN:
        window_end = window_start + timedelta(seconds=BATCH_SECONDS)
        if window_end > horizon:
            break                          # the window is not closed yet

        records = list((await session.execute(
            select(AIDecisionRecordModel).where(
                AIDecisionRecordModel.chain_sequence.is_(None),
                AIDecisionRecordModel.recorded_at >= window_start,
                AIDecisionRecordModel.recorded_at < window_end,
            ).execution_options(populate_existing=True)
        )).scalars().all())
        verdicts = list((await session.execute(
            select(AIDecisionVerdictModel).where(
                AIDecisionVerdictModel.chain_sequence.is_(None),
                AIDecisionVerdictModel.decided_at >= window_start,
                AIDecisionVerdictModel.decided_at < window_end,
            ).execution_options(populate_existing=True)
        )).scalars().all())

        sequence += 1
        content_hash = compute_batch_hash(
            prev_hash, records, verdicts, window_start, window_end)
        session.add(AILedgerChainModel(
            id=uuid.uuid4(),
            sequence=sequence,
            window_start=window_start,
            window_end=window_end,
            record_count=len(records),
            verdict_count=len(verdicts),
            prev_hash=prev_hash,
            content_hash=content_hash,
            sealed_at=_utcnow(),
        ))
        if records:
            await session.execute(
                update(AIDecisionRecordModel)
                .where(AIDecisionRecordModel.id.in_([r.id for r in records]))
                .values(chain_sequence=sequence))
        if verdicts:
            await session.execute(
                update(AIDecisionVerdictModel)
                .where(AIDecisionVerdictModel.id.in_([v.id for v in verdicts]))
                .values(chain_sequence=sequence))
        await session.flush()

        prev_hash = content_hash
        window_start = window_end
        sealed += 1

    if sealed:
        await session.commit()
    if sealed >= MAX_BATCHES_PER_RUN:
        log.warning("ai-ledger: hit the %d-batch ceiling in one pass; the "
                    "backlog continues on the next run", MAX_BATCHES_PER_RUN)
    return sealed


_VERIFY_BLOCK = 5000


async def verify_chain(session: AsyncSession) -> tuple[bool, str | None]:
    """Recompute every link. Returns (ok, first problem or a pruning note).

    If this returns False, the ledger has been altered since it was sealed.
    """
    batches = (await session.execute(
        select(AILedgerChainModel).order_by(AILedgerChainModel.sequence)
        .execution_options(populate_existing=True)
    )).scalars().all()

    prev_hash = GENESIS
    expected_seq = 1
    pruned: list[dict] = []
    # Rows are read a block of batches at a time (2026-11-04): two queries per
    # batch took 30 s on 89k batches — empty windows are sealed too, so the chain
    # grows by the minute. Same rows, same `populate_existing`, same hash.
    block: dict[int, tuple[list, list]] = {}
    block_end = 0
    for batch in batches:
        if batch.sequence > block_end:
            block_start, block_end = batch.sequence, batch.sequence + _VERIFY_BLOCK - 1
            block = {}
            for model, slot in ((AIDecisionRecordModel, 0), (AIDecisionVerdictModel, 1)):
                rows = (await session.execute(
                    select(model)
                    .where(model.chain_sequence.between(block_start, block_end))
                    .execution_options(populate_existing=True)
                )).scalars().all()
                for row in rows:
                    block.setdefault(row.chain_sequence, ([], []))[slot].append(row)
        if batch.sequence != expected_seq:
            return False, (f"sequence jumps from {expected_seq - 1} to "
                           f"{batch.sequence}: a sealed batch is missing")
        if batch.prev_hash != prev_hash:
            return False, f"batch {batch.sequence} does not link to the one before it"

        if batch.pruned_at is not None:
            # Content deleted by retention, deliberately and recorded. The LINK
            # is still checked above and below, so the chain keeps proving that
            # nothing before or after was edited. Re-sealing over the new
            # contents instead would let anyone erase evidence and leave a
            # chain saying everything is fine.
            pruned.append({"sequence": batch.sequence,
                           "pruned_at": batch.pruned_at,
                           "removed": batch.pruned_count})
            prev_hash = batch.content_hash
            expected_seq += 1
            continue

        # `populate_existing` refreshes from the database instead of handing
        # back what this session already holds. Without it a verifier run on a
        # session that has the rows cached checks the cache, not the record.
        records, verdicts = block.get(batch.sequence, ([], []))
        if len(records) != batch.record_count or len(verdicts) != batch.verdict_count:
            return False, (
                f"batch {batch.sequence} sealed {batch.record_count} record(s) "
                f"and {batch.verdict_count} verdict(s) and now holds "
                f"{len(records)}/{len(verdicts)}. If this was a retention "
                f"purge, the batch was not tombstoned — which is a bug, not a "
                f"purge.")

        ws = (batch.window_start if batch.window_start.tzinfo
              else batch.window_start.replace(tzinfo=timezone.utc))
        we = (batch.window_end if batch.window_end.tzinfo
              else batch.window_end.replace(tzinfo=timezone.utc))
        if compute_batch_hash(prev_hash, records, verdicts, ws, we) != batch.content_hash:
            return False, f"batch {batch.sequence} does not match what was sealed"

        prev_hash = batch.content_hash
        expected_seq += 1

    if pruned:
        total = sum(p["removed"] or 0 for p in pruned)
        return True, (
            f"verified; {len(pruned)} batch(es) were pruned by retention, "
            f"removing {total} record(s). Their content can no longer be "
            f"re-derived — the chain still proves nothing was edited.")
    return True, None


async def purge_before(
    session: AsyncSession, *, before: datetime, dry_run: bool = False,
) -> dict[str, int]:
    """Delete sealed ledger rows older than `before`, tombstoning their batches.

    Two refusals, both load-bearing:

    * **An unsealed row is never purged.** It is not covered by any batch, so
      deleting it leaves a hole nothing records — and a hole in the windows is
      how removal is detected.
    * **A batch that loses rows without a tombstone is tampering.** So the
      tombstone is written in the same transaction as the delete, never after.
    """
    doomed = list((await session.execute(
        select(AIDecisionRecordModel).where(
            AIDecisionRecordModel.chain_sequence.is_not(None),
            AIDecisionRecordModel.recorded_at < before,
        )
    )).scalars().all())
    if not doomed:
        return {"records": 0, "batches": 0}

    per_batch: dict[int, int] = {}
    for r in doomed:
        per_batch[r.chain_sequence] = per_batch.get(r.chain_sequence, 0) + 1

    if dry_run:
        return {"records": len(doomed), "batches": len(per_batch)}

    ids = [r.id for r in doomed]
    # Verdicts go with their record by FK cascade, which is why they are not
    # deleted separately here — and why their counts are folded into the
    # tombstone rather than checked afterwards.
    await session.execute(
        delete(AIDecisionRecordModel).where(AIDecisionRecordModel.id.in_(ids)))

    for seq, count in per_batch.items():
        batch = (await session.execute(
            select(AILedgerChainModel).where(AILedgerChainModel.sequence == seq)
        )).scalars().first()
        if batch is None:                  # pragma: no cover — defensive
            continue
        batch.pruned_at = _utcnow()
        batch.pruned_count = (batch.pruned_count or 0) + count
    await session.commit()
    log.info("ai-ledger: purged %d record(s) across %d batch(es), tombstoned",
             len(doomed), len(per_batch))
    return {"records": len(doomed), "batches": len(per_batch)}


async def sealer_loop() -> None:                              # pragma: no cover
    """Background task. Failure is logged and retried, never fatal.

    A sealer that dies takes the evidence with it, not the platform: records
    keep being written and are sealed late when it comes back, because
    `seal_once` resumes from the tip rather than from now.
    """
    from velaris_shared.db.session import get_session_factory

    while True:
        try:
            async with get_session_factory()() as session:
                n = await seal_once(session)
                if n:
                    log.info("ai-ledger: sealed %d batch(es)", n)
        except asyncio.CancelledError:
            raise
        except Exception:                                      # noqa: BLE001
            log.warning("ai-ledger: sealing pass failed", exc_info=True)
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)
