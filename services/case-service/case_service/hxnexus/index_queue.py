"""Document indexing, off the request.

**The problem.** `index_document` extracts text, and since OCR was wired
(2026-09-09) extracting text from a scanned page can take
`documents.ocr.OCR_TIMEOUT_SECONDS`. It is awaited INSIDE the request, and
`POST /hxnexus/cases/{id}/documents/index` loops every document on the case, so
that timeout is multiplied by the number of scans rather than paid once. The
timeout was halved from 60 to 30 to bound the damage. Bounding is not fixing.

**The shape.** A queue table (migration 203) and a `document_index` scheduler
action that drains it — the same choice `calendar_feed` and `audit_stream` made,
for the same reason: a second always-on loop is a second thing to configure, a
second thing to forget, and a second place for a stall to hide.

**What does not change.** The synchronous path is untouched and is still the
default. A caller gets the background path only by asking for it
(`background: true`), and the queue is drained only if an operator created a
schedule. An install that takes this update and does neither indexes exactly as
it does today.

**Access is checked at ENQUEUE, not at drain.** The runner has no caller — it
cannot re-ask HxGuard whether somebody may read a case. So the route performs
the same `_assert_case_access` the synchronous path performs, and the row
records the tenant and the requester. Two consequences, both deliberate: a job
enqueued by someone who later loses access still runs (it is work they were
entitled to ask for at the time, and it writes chunks, never a reply to them),
and nothing in this module may ever be reachable by a caller who did not go
through that route.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

#: Jobs one drain may claim. A drain that never returns is a stalled schedule.
DEFAULT_MAX_JOBS = 25

#: Seconds one drain may spend. Checked BETWEEN jobs, never inside one — a
#: half-indexed document is worse than a long run, and each document already
#: carries its own OCR timeout.
DEFAULT_BUDGET_SECONDS = 300

#: The statuses that mean "this job still has work to do".
ACTIVE_STATUSES = ("pending", "running")

#: After this long, a `running` job is assumed abandoned and returns to
#: `pending`. A drain that is killed mid-document — a restart, an OOM — would
#: otherwise leave the row `running` forever, and because `active_key` is still
#: set, that document could never be queued again either. A dedupe key without a
#: reclaim is a permanent block, not a safeguard.
RECLAIM_AFTER_SECONDS = 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    case_id: uuid.UUID | None,
    tenant_id: str | None,
    requested_by: str | None,
    reindex: bool = False,
    staged: dict[str, str] | None = None,
) -> tuple[Any, bool]:
    """Queue one document. Returns `(row, created)`.

    An already-queued document is NOT queued twice — the caller gets the
    existing row back and `created=False`. Enqueuing is what a screen does on a
    button press, and a double click must not double the OCR bill.
    """
    from velaris_shared.db.models import DocumentIndexJobModel

    existing = (await session.execute(
        select(DocumentIndexJobModel).where(
            DocumentIndexJobModel.active_key == str(document_id)
        )
    )).scalar_one_or_none()
    if existing is not None:
        # `reindex` is widened, never narrowed: if either the queued request or
        # this one wants the old chunks dropped, they are dropped. The opposite
        # would let a second, cheaper request silently cancel the first's intent.
        if reindex and not existing.reindex:
            existing.reindex = True
            await session.commit()
        return existing, False

    try:
        return await _insert(session, DocumentIndexJobModel,
                             document_id=document_id, case_id=case_id,
                             tenant_id=tenant_id, requested_by=requested_by,
                             reindex=reindex, staged=staged), True
    except IntegrityError:
        # Two enqueues of the same document at once: the unique index on
        # `active_key` is what decides, not the SELECT above, which can be stale
        # by the time the INSERT lands. The loser reads the winner's row and
        # reports "already queued" — the same answer it would have given a
        # moment earlier.
        await session.rollback()
        existing = (await session.execute(
            select(DocumentIndexJobModel).where(
                DocumentIndexJobModel.active_key == str(document_id)
            )
        )).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False


async def _insert(session: AsyncSession, DocumentIndexJobModel, *, document_id,
                  case_id, tenant_id, requested_by, reindex, staged=None):
    staged = staged or {}
    row = DocumentIndexJobModel(
        id=uuid.uuid4(),
        # `cases.tenant_id` is a GUID column and this one is a string, so the
        # value arrives as a UUID object from the route and as a string from a
        # script. Coerced once, here, rather than at each caller.
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        case_id=case_id,
        document_id=document_id,
        status="pending",
        active_key=str(document_id),
        reindex=reindex,
        requested_by=requested_by,
        staged_storage_key=staged.get("storage_key"),
        staged_filename=staged.get("filename"),
        staged_content_type=staged.get("content_type"),
        created_at=_utcnow(),
    )
    session.add(row)
    await session.commit()
    return row


async def _claim(session: AsyncSession, job) -> bool:
    """Move one pending job to running, and say whether WE moved it.

    The guard is in the WHERE clause, not in Python: two schedules draining at
    once must not both run the same document, and `SELECT` then `UPDATE` without
    a status condition is exactly how they would.
    """
    from velaris_shared.db.models import DocumentIndexJobModel

    result = await session.execute(
        update(DocumentIndexJobModel)
        .where(
            DocumentIndexJobModel.id == job.id,
            DocumentIndexJobModel.status == "pending",
        )
        .values(status="running", started_at=_utcnow(),
                attempts=DocumentIndexJobModel.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if not result.rowcount:
        return False
    # Re-read: `attempts` was incremented in SQL, and the retry decision in
    # `run_one` compares it against `max_attempts`. Reading the stale ORM value
    # buys the job one extra attempt — one extra OCR timeout — every time.
    await session.refresh(job)
    return True


async def _finish(session: AsyncSession, job_id, document_id, *, status: str,
                  chunks: int | None = None, error: str | None = None) -> None:
    """Record an outcome by id, never through the ORM object.

    This is called from a failure handler that has just rolled back, and a
    rollback expires every loaded attribute — reading `job.id` there raises
    MissingGreenlet from inside the code meant to record why something failed.
    """
    from velaris_shared.db.models import DocumentIndexJobModel

    values: dict[str, Any] = {
        "status": status, "finished_at": _utcnow(),
        "chunks_indexed": chunks, "last_error": error,
    }
    if status != "pending":
        # Done, failed or cancelled: the document is free to be queued again.
        values["active_key"] = None
    else:
        values["active_key"] = str(document_id)
        values["finished_at"] = None
    await session.execute(
        update(DocumentIndexJobModel)
        .where(DocumentIndexJobModel.id == job_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _refused_now(session: AsyncSession, case_id) -> str | None:
    """Why this case must not be indexed *now*, or None to go ahead.

    **Erasure is the reason this exists.** Indexing writes the document's text
    into `hxnexus_document_chunks` — a derived copy that HxNexus can read back.
    A case anonymized between enqueue and drain would otherwise have that copy
    created *after* it was erased, by a job queued before. A background path
    that can un-erase a case is not a background path anybody wants.

    The check is `allows_case_purpose`, the same gate `core/ai_action.py` uses,
    which refuses an anonymized case outright and otherwise asks whether the
    case's subject has a lawful basis for AI processing. A case that has been
    deleted since is refused here too — `allows_case_purpose` returns
    `unknown_case`, and an unresolvable case is not evidence of anything.

    **The synchronous route is deliberately NOT changed to match.** Adding a
    refusal there would change what an existing install does on upgrade, which
    is not an update's decision to make. It is named as a gap in the component
    doc instead.
    """
    from case_service.compliance import consent as consent_svc

    try:
        allowed, reason = await consent_svc.allows_case_purpose(
            session, case_id, consent_svc.AI_PROCESSING)
    except Exception as exc:  # noqa: BLE001 — a gate that opens on error is not a gate
        log.warning("index queue consent check failed on case %s: %s", case_id, exc)
        allowed, reason = False, "check_failed"
    if allowed:
        return None
    return f"refused: no lawful basis for ai_processing ({reason})"


async def run_one(session: AsyncSession, job) -> dict:
    """Index one claimed job's document. Never raises — it records instead."""
    from velaris_shared.db.models import DocumentModel, DocumentVersionModel, DocumentChunkModel
    from case_service.hxnexus.service import index_document, not_embedded_note
    report: dict = {}
    from case_service.storage import get_storage_backend

    # Read off the row BEFORE any rollback. A rollback expires every loaded
    # attribute, and touching an expired one on an AsyncSession outside a
    # greenlet is a MissingGreenlet — an error about SQLAlchemy internals raised
    # from the failure handler, which is the worst place to lose the real cause.
    job_id, document_id = job.id, job.document_id
    job_case_id, job_tenant_id = job.case_id, job.tenant_id
    attempts, max_attempts = job.attempts, job.max_attempts
    staged_key = getattr(job, "staged_storage_key", None)
    staged_type = getattr(job, "staged_content_type", None)
    staged_name = getattr(job, "staged_filename", None)

    # ── What may have changed since this job was queued ──────────────────
    #
    # The drain runs later, and "later" is the whole risk of a queue. Access was
    # checked at enqueue and cannot be re-checked (there is no caller), but the
    # facts that make indexing lawful and useful CAN be re-read, and each of
    # these was true at enqueue and may not be now.
    if job_case_id is not None:
        refusal = await _refused_now(session, job_case_id)
        if refusal is not None:
            await _finish(session, job_id, document_id, status="failed",
                          chunks=0, error=refusal)
            await discard_staged(staged_key)
            return {"document_id": str(document_id), "status": "failed",
                    "error": refusal}

    if staged_key:
        return await _run_staged(session, job_id, document_id, job_case_id,
                                 job_tenant_id, staged_key, staged_type,
                                 staged_name, attempts, max_attempts)

    doc = (await session.execute(
        select(DocumentModel).where(DocumentModel.id == document_id)
    )).scalar_one_or_none()
    if doc is not None and getattr(doc, "is_deleted", False):
        # Deleted between enqueue and drain. Indexing it would put its text back
        # into the chunk table, which is the one place a deleted document's
        # content could still be read out — and HxNexus would then answer from
        # a document a person deliberately removed.
        await _finish(session, job_id, document_id, status="failed", chunks=0,
                      error="Document was deleted before it could be indexed.")
        return {"document_id": str(document_id), "status": "failed",
                "error": "Document was deleted before it could be indexed."}
    if doc is None:
        # A document deleted between enqueue and drain is not an error worth
        # retrying — it is a job with nothing left to do.
        await _finish(session, job_id, document_id, status="failed", chunks=0,
                      error="Document no longer exists.")
        return {"document_id": str(document_id), "status": "failed",
                "error": "Document no longer exists."}

    ver = (await session.execute(
        select(DocumentVersionModel)
        .where(DocumentVersionModel.document_id == doc.id)
        .order_by(DocumentVersionModel.version.desc())
        .limit(1)
    )).scalar_one_or_none()
    if ver is None:
        await _finish(session, job_id, document_id, status="failed", chunks=0,
                      error="Document has no stored version.")
        return {"document_id": str(document_id), "status": "failed",
                "error": "Document has no stored version."}

    try:
        if job.reindex:
            stale = (await session.execute(
                select(DocumentChunkModel).where(
                    DocumentChunkModel.document_id == doc.id)
            )).scalars().all()
            for chunk in stale:
                await session.delete(chunk)
            await session.flush()

        data = await get_storage_backend().get(ver.storage_key)
        chunks = await index_document(
            session, doc.id, job_case_id, data, doc.content_type, job_tenant_id,
            report=report,
        )
    except Exception as exc:  # noqa: BLE001 — the outcome is a row, not a 500
        await session.rollback()
        message = f"{type(exc).__name__}: {exc}"[:2000]
        log.warning("Document index job %s failed: %s", job_id, message)
        # Back to pending while attempts remain: a storage blip and an
        # unreadable file are the same exception here, and only one of them is
        # worth another OCR timeout — which is what `max_attempts` decides.
        exhausted = attempts >= max_attempts
        await _finish(session, job_id, document_id, status="failed" if exhausted else "pending",
                      error=message)
        return {"document_id": str(document_id),
                "status": "failed" if exhausted else "pending",
                "error": message}

    # Done, but said: chunks no embedding model reached are on the row (R4).
    note = not_embedded_note(report)
    await _finish(session, job_id, document_id, status="done", chunks=chunks, error=note)
    return {"document_id": str(document_id), "status": "done",
            "filename": doc.filename, "chunks": chunks,
            **({"warning": note} if note else {})}


def staged_key_for(document_id) -> str:
    """Where a background upload's bytes wait. One fixed prefix, so an operator
    can see (and a sweep can find) everything the queue is holding."""
    return f"hxnexus/staged-uploads/{document_id}"


async def discard_staged(key: str | None) -> None:
    """Delete a staged upload. Never raises: a failed delete is logged, and the
    job's outcome is already recorded — losing it over cleanup would be worse."""
    if not key:
        return
    from case_service.storage import get_storage_backend

    try:
        await get_storage_backend().delete(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not delete staged upload %s: %s", key, exc)


async def abandon_staged(session: AsyncSession, job_id, document_id, why: str) -> None:
    """End a staged job whose bytes never arrived — cancelled, with the reason."""
    await _finish(session, job_id, document_id, status="cancelled", chunks=0,
                  error=f"cancelled: {why}")
    await discard_staged(staged_key_for(document_id))


async def _run_staged(session: AsyncSession, job_id, document_id, case_id, tenant_id,
                      key, content_type, filename, attempts, max_attempts) -> dict:
    """Index a background upload from its staged bytes (mig 206).

    There is no document row: the synchronous upload never made one either, and
    the chunks land under the same fresh `document_id` it would have used. The
    staged bytes are deleted when the job ENDS (done, or failed for good) — they
    are the customer's file, kept only as long as the work needs them. A retry
    that is still pending keeps them.
    """
    from case_service.hxnexus.service import index_document, not_embedded_note
    report: dict = {}
    from case_service.storage import get_storage_backend

    try:
        data = await get_storage_backend().get(key)
        chunks = await index_document(
            session, document_id, case_id, data,
            content_type or "application/octet-stream", tenant_id, report=report,
        )
    except Exception as exc:  # noqa: BLE001 — the outcome is a row, not a 500
        await session.rollback()
        message = f"{type(exc).__name__}: {exc}"[:2000]
        log.warning("Document index job %s (staged upload) failed: %s", job_id, message)
        exhausted = attempts >= max_attempts
        await _finish(session, job_id, document_id,
                      status="failed" if exhausted else "pending", error=message)
        if exhausted:
            await discard_staged(key)
        return {"document_id": str(document_id),
                "status": "failed" if exhausted else "pending", "error": message}

    note = not_embedded_note(report)
    await _finish(session, job_id, document_id, status="done", chunks=chunks, error=note)
    await discard_staged(key)
    return {"document_id": str(document_id), "status": "done",
            "filename": filename, "chunks": chunks,
            **({"warning": note} if note else {})}


async def reclaim_stale(session: AsyncSession,
                        older_than_seconds: float = RECLAIM_AFTER_SECONDS) -> int:
    """Return abandoned `running` jobs to `pending`. Answers how many.

    A drain killed mid-document — a restart, an OOM, a machine going away —
    leaves its row `running` with `active_key` still set. Nothing would ever
    pick it up again, and because the key is what stops a document being queued
    twice, that document could never be queued again either: a permanent block
    produced by the safeguard against duplicates.

    Attempts are NOT incremented here. The attempt was already counted when the
    job was claimed, and counting it twice would burn a document's retries on
    an outage that had nothing to do with it.
    """
    from velaris_shared.db.models import DocumentIndexJobModel

    cutoff = _utcnow() - timedelta(seconds=older_than_seconds)
    result = await session.execute(
        update(DocumentIndexJobModel)
        .where(
            DocumentIndexJobModel.status == "running",
            DocumentIndexJobModel.started_at.isnot(None),
            DocumentIndexJobModel.started_at < cutoff,
        )
        .values(status="pending")
        # The comparison belongs to the DATABASE. SQLAlchemy's default is to
        # re-evaluate the criteria in Python against rows already loaded in the
        # session, and a `started_at` that came back from SQLite is naive while
        # `cutoff` is aware — "can't compare offset-naive and offset-aware
        # datetimes", raised from inside a housekeeping call. The same
        # naive/aware split once turned a three-second credential lease into a
        # two-hour one.
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount:
        log.warning("index queue: reclaimed %s stale running job(s)", result.rowcount)
    return int(result.rowcount or 0)


async def run_due_jobs(
    session: AsyncSession,
    *,
    max_jobs: int = DEFAULT_MAX_JOBS,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    tenant_id: str | None = None,
) -> dict:
    """Drain pending jobs. Bounded by BOTH a count and a wall clock.

    `tenant_id` is a caller's tenant TOKEN — a UUID on some installs and a
    directory slug on others — while the row holds the case's tenant GUID. They
    are compared with `same_tenant`, not with `==`: comparing a slug to a GUID
    is how `costing._authorized_case` came to answer "case not found" to a case's
    own tenant, silently, for every tenanted install. That resolver needs the
    session, so the tenant test happens in Python over a page of candidates
    rather than in the WHERE clause.
    """
    from velaris_shared.db.models import DocumentIndexJobModel
    from velaris_shared.tenancy.identity import same_tenant

    started = time.monotonic()
    await reclaim_stale(session)

    query = (
        select(DocumentIndexJobModel)
        .where(DocumentIndexJobModel.status == "pending")
        .order_by(DocumentIndexJobModel.created_at)
        # Over-read when a tenant filter will thin the page, so one tenant's
        # backlog cannot starve another's schedule of its whole allowance.
        .limit(max_jobs if tenant_id is None else max_jobs * 5)
    )
    jobs = (await session.execute(query)).scalars().all()

    results: list[dict] = []
    stopped_early = False
    for job in jobs:
        if len(results) >= max_jobs:
            break
        if time.monotonic() - started > budget_seconds:
            stopped_early = True
            break
        # A NULL tenant has always meant "platform-shared" here, and
        # `same_tenant` returns True for it — a drain that skipped those rows
        # would leave them queued forever.
        if tenant_id is not None and not await same_tenant(
                session, job.tenant_id, tenant_id):
            continue
        if not await _claim(session, job):
            continue  # another drain took it
        try:
            results.append(await run_one(session, job))
        except Exception as exc:  # noqa: BLE001
            # `run_one` records its own outcomes; reaching here means something
            # outside its try failed — a dead connection, most likely. Leave the
            # row `running` and let `reclaim_stale` pick it up rather than
            # guessing at a status with a session that may be unusable, but do
            # not let one job stop the drain.
            log.warning("index queue job %s raised outside its handler: %s",
                        job.id, exc)
            results.append({"document_id": str(job.document_id),
                            "status": "running", "error": str(exc)[:500]})

    return {
        "claimed": len(results),
        "indexed": sum(1 for r in results if r["status"] == "done"),
        "chunks": sum(r.get("chunks", 0) or 0 for r in results),
        "failed": [r for r in results if r["status"] != "done"],
        "stopped_on_budget": stopped_early,
        "details": results,
    }


def as_dict(job) -> dict:
    """The row as an API answer. `last_error` is included on purpose: a queue
    that says only 'failed' sends somebody to the server logs."""
    return {
        "id": str(job.id),
        "document_id": str(job.document_id),
        "case_id": str(job.case_id) if job.case_id else None,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "reindex": bool(job.reindex),
        "chunks_indexed": job.chunks_indexed,
        "last_error": job.last_error,
        "requested_by": job.requested_by,
        # A background upload (mig 206): no document row, so its name is here.
        "staged_upload": bool(getattr(job, "staged_storage_key", None)),
        "staged_filename": getattr(job, "staged_filename", None),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
