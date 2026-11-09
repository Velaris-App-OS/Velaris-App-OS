"""HxNexus AI Copilot API — P30.

Endpoints:
  GET    /hxnexus/status                              backend health + model info
  POST   /hxnexus/cases/{id}/suggest                  next-best-action
  POST   /hxnexus/cases/{id}/documents/index          index case documents
  GET    /hxnexus/cases/{id}/documents/index-jobs     the background queue for a case
  POST   /hxnexus/cases/{id}/documents/index-jobs/{job}/cancel   take one back out
  POST   /hxnexus/documents/upload                    upload + index a document directly
  POST   /hxnexus/cases/{id}/qa                       document Q&A (RAG)
  POST   /hxnexus/chat                                start or continue a conversation
  GET    /hxnexus/conversations                        list own conversations
  GET    /hxnexus/conversations/{id}/messages          conversation history
  GET    /hxnexus/conversations/{id}/transcript        plain-text transcript download
  POST   /hxnexus/conversations/{id}/summarize         LLM-generated summary
  DELETE /hxnexus/conversations/{id}                  delete conversation
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CaseInstanceModel, DocumentModel, DocumentVersionModel,
    CopilotConversationModel, CopilotMessageModel,
    CaseAssignmentModel,
)
from velaris_shared.db.session import get_session
from case_service.tenancy.identity import same_tenant
from case_service.agents.attribution import AgentRefused   # F-00: policy refusal is 403
from velaris_shared.llm.factory import get_llm_backend, get_ai_info, check_ai_available
from velaris_shared.llm.guard import (
    chat_rate_limiter, regen_rate_limiter, global_rate_limiter,
    validate_message_length, scan_input,
)
from case_service.hxnexus.service import (
    next_best_action, qa_over_documents, chat, index_document,
)
from case_service.storage import get_storage_backend
from velaris_shared.db.read_routing import get_read_session

log = logging.getLogger(__name__)

from velaris_shared import hxguard

router = APIRouter(prefix="/hxnexus", tags=["hxnexus"])


# ── Shared guards ─────────────────────────────────────────────────────

def _check_global_rate(user: AuthenticatedUser) -> None:
    """Apply the global per-user HxNexus rate limit."""
    allowed, retry = global_rate_limiter.is_allowed(str(user.user_id))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in {retry}s.",
            headers={"Retry-After": str(retry)},
        )


async def _assert_case_access(
    case_id: uuid.UUID,
    user: AuthenticatedUser,
    session: AsyncSession,
) -> CaseInstanceModel:
    """Load a case and verify the requesting user has access to it.

    Admins and managers can access any case. Case workers must be assigned
    to the case or have created it.
    """
    from case_service.tenancy.identity import case_visible_to, is_superadmin

    case = await session.get(CaseInstanceModel, case_id)
    if not case:
        raise coded(404, LLM_NOTFOUND_404, "Case not found")

    # The tenant boundary, FIRST — before any role can widen past it.
    #
    # "Admins and managers have platform-wide case access" was written when this
    # platform had one tenant, and `platform-wide` quietly came to mean *across
    # tenants*: on a shared install `admin` is a TENANT admin. Proven on
    # 2026-09-06 — a `dev` admin asked HxNexus to summarise a case belonging to
    # tenant `default` and got back its status, its priority and the contents of
    # a note marked confidential. That is worse than reading the row, because an
    # answer is not bounded by the fields a screen would have shown.
    #
    # A case in another tenant is 404 — the same answer as one that does not
    # exist — and superadmin keeps the documented break-glass.
    if not await case_visible_to(session, case.tenant_id,
                                 getattr(user, "tenant_id", None),
                                 is_superadmin_caller=is_superadmin(user)):
        raise coded(404, LLM_NOTFOUND_404, "Case not found")

    # Admins and managers have platform-wide case access
    # Phase 4 step 2: `has_role("manager")` also returned True for any admin
    # (has_role passes on is_admin), so the two clauses overlapped. They are now
    # distinct: the privilege, or the manager role held literally.
    if (hxguard.user_allows(user, "casework.sensitive.read")
            or "manager" in (user.roles or [])):
        return case

    # Case worker: must be creator or have an active assignment
    if str(getattr(case, "created_by", "")) == str(user.user_id):
        return case

    assignment = (await session.execute(
        select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id,
            CaseAssignmentModel.assignee_id == str(user.user_id),
            CaseAssignmentModel.status == "active",
        ).limit(1)
    )).scalar_one_or_none()

    if assignment is None:
        raise coded(403, LLM_NOT_ASSIGNED_403, "You are not assigned to this case.")

    return case


# ─── Pydantic schemas ────────────────────────────────────────────────

class QARequest(BaseModel):
    question: str
    top_k: int = 5
    use_cloud: bool = False  # Group H: explicit per-query opt-in to the external provider


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[uuid.UUID] = None
    case_id: Optional[uuid.UUID] = None
    use_cloud: bool = False  # Group H: explicit per-query opt-in to the external provider


class IndexRequest(BaseModel):
    reindex: bool = False
    #: Queue the work instead of doing it in this request (migration 203).
    #: **Default False — the synchronous path, exactly as before.** With OCR
    #: enabled, indexing a case full of scans holds the request for
    #: `OCR_TIMEOUT_SECONDS` per document; `background: true` returns a job per
    #: document instead, drained by a `document_index` schedule.
    background: bool = False


# ─── Status (real async liveness check) ──────────────────────────────

@router.get("/llmops/metrics")
async def llmops_metrics(
    group_by: str = "model",
    hours: int | None = None,
    session: AsyncSession = Depends(get_read_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("ai.metrics.read")),
):
    """Per-call AI metrics — cost, volume, latency, error rate (#32, §5.4).

    The platform operator sees every call; a tenant's admin its own tenant's
    (2026-11-04: it answered every tenant's admin with the whole install's).

    Grouped by `model`, `provider`, `purpose` or `route`; `hours` limits the
    window. Token counts are estimated from character length — no backend here
    reports provider usage — so the response says so rather than presenting a
    guess as an invoice.
    """
    from datetime import datetime, timedelta, timezone

    from velaris_shared.llm import llmops

    since = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
        if hours and hours > 0 else None
    )
    try:
        from case_service.tenancy.identity import (
            caller_tenant_tokens, is_platform_operator, same_tenant_as_default,
        )
        if await is_platform_operator(session, _user):
            return await llmops.metrics(session, since=since, group_by=group_by)
        tokens = await caller_tenant_tokens(session, _user) or []
        return await llmops.metrics(
            session, since=since, group_by=group_by, tenants=[str(t) for t in tokens],
            include_untenanted=await same_tenant_as_default(session, _user.tenant_id or "default"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/status")
async def hxnexus_status(_: AuthenticatedUser = Depends(get_current_user)):
    """Return HxNexus availability and active backend info."""
    available = await check_ai_available()
    info = get_ai_info()
    return {
        "name": "HxNexus",
        "backend": info["backend"],
        "available": available,
        # None when healthy. Non-null means the configured provider was dropped
        # for a named reason and the local model is answering in its place —
        # without this, `backend: "ollama"` and `available: true` look like a
        # deliberate local-only install rather than a broken external one.
        "misconfigured": info["misconfigured"],
        "config": info["config"],
        "capabilities": ["next_best_action", "document_qa", "chat", "transcript", "summarize"],
    }


# ─── Next-best-action ────────────────────────────────────────────────

@router.post("/cases/{case_id}/suggest")
async def suggest(
    case_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _check_global_rate(current_user)
    case = await _assert_case_access(case_id, current_user, session)
    # case_vars façade read: typed pii/secret variables reach the AI
    # provider masked — model egress is never a privileged reader.
    from case_service.case_vars import service as case_vars
    vars_ctx = case_vars.CallerContext(kind="platform", actor_id=current_user.user_id)
    case_data = {
        "id": str(case.id), "status": case.status,
        "priority": case.priority,
        "data": await case_vars.get_all(session, vars_ctx, case.id),
    }
    suggestions = await next_best_action(case_data)
    return {"case_id": str(case_id), "suggestions": suggestions}


# ─── Document indexing ────────────────────────────────────────────────

@router.post("/cases/{case_id}/documents/index")
async def index_case_documents(
    case_id: uuid.UUID,
    body: IndexRequest = IndexRequest(),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Fetch all documents for a case, extract text, embed, and store chunks."""
    from velaris_shared.db.models import DocumentChunkModel

    _check_global_rate(current_user)
    case = await _assert_case_access(case_id, current_user, session)

    if body.reindex:
        existing = (await session.execute(
            select(DocumentChunkModel).where(DocumentChunkModel.case_id == case_id)
        )).scalars().all()
        for c in existing:
            await session.delete(c)
        await session.flush()

    docs = (await session.execute(
        select(DocumentModel).where(DocumentModel.case_id == case_id)
    )).scalars().all()

    if body.background:
        # Access was checked above, by the same `_assert_case_access` the
        # synchronous path uses; the queue row carries the tenant and the
        # requester, because the drain has no caller to ask.
        from case_service.hxnexus import index_queue

        queued, already = [], []
        for doc in docs:
            job, created = await index_queue.enqueue_document(
                session,
                document_id=doc.id,
                case_id=case_id,
                tenant_id=getattr(case, "tenant_id", None),
                requested_by=current_user.user_id,
                reindex=bool(body.reindex),
            )
            (queued if created else already).append(index_queue.as_dict(job))
        return {
            "case_id": str(case_id), "queued": len(queued),
            "already_queued": len(already), "jobs": queued + already,
            "note": ("Queued. These are indexed by a `document_index` schedule; "
                     "without one, nothing drains the queue."),
        }

    storage = get_storage_backend()
    total_chunks = 0
    not_embedded = 0
    results = []

    for doc in docs:
        ver = (await session.execute(
            select(DocumentVersionModel)
            .where(DocumentVersionModel.document_id == doc.id)
            .order_by(DocumentVersionModel.version.desc())
            .limit(1)
        )).scalar_one_or_none()
        if ver is None:
            continue
        try:
            data = await storage.get(ver.storage_key)
        except Exception:
            continue
        report: dict = {}
        n = await index_document(
            session, doc.id, case_id, data,
            doc.content_type, getattr(case, "tenant_id", None), report=report,
        )
        total_chunks += n
        not_embedded += report.get("not_embedded", 0)
        results.append({"document_id": str(doc.id), "filename": doc.filename, "chunks": n})

    from case_service.hxnexus.service import not_embedded_note
    out = {"case_id": str(case_id), "documents_indexed": len(results),
           "total_chunks": total_chunks, "details": results}
    if note := not_embedded_note({"not_embedded": not_embedded,
                                  "embedded": total_chunks - not_embedded}):
        out["warning"] = note
    return out


@router.get("/cases/{case_id}/documents/index-jobs")
async def list_index_jobs(
    case_id: uuid.UUID,
    status: Optional[str] = Query(None, description="pending|running|done|failed|cancelled"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """What the queue is doing for this case.

    A queue with no window on it is indistinguishable from a document that was
    silently dropped — which is the failure a background path introduces and
    this endpoint exists to prevent. Gated by the same case access as indexing:
    a job row names a document on the case.
    """
    from velaris_shared.db.models import DocumentIndexJobModel
    from case_service.hxnexus import index_queue

    _check_global_rate(current_user)
    await _assert_case_access(case_id, current_user, session)

    query = (select(DocumentIndexJobModel)
             .where(DocumentIndexJobModel.case_id == case_id)
             .order_by(DocumentIndexJobModel.created_at.desc())
             .limit(200))
    if status:
        query = query.where(DocumentIndexJobModel.status == status.strip().lower())
    rows = (await session.execute(query)).scalars().all()
    return {"case_id": str(case_id), "count": len(rows),
            "jobs": [index_queue.as_dict(r) for r in rows]}


@router.post("/cases/{case_id}/documents/index-jobs/{job_id}/cancel")
async def cancel_index_job(
    case_id: uuid.UUID,
    job_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Take a queued job back out. Only a job that has not started yet."""
    from velaris_shared.db.models import DocumentIndexJobModel

    _check_global_rate(current_user)
    await _assert_case_access(case_id, current_user, session)

    # The case clause is the tenant boundary here: access to the case was just
    # checked, and a job id from another case answers 404, never 403.
    job = (await session.execute(
        select(DocumentIndexJobModel).where(
            DocumentIndexJobModel.id == job_id,
            DocumentIndexJobModel.case_id == case_id,
        )
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Index job not found")
    if job.status != "pending":
        raise HTTPException(409, f"Job is {job.status}, not pending")

    job.status = "cancelled"
    job.active_key = None          # frees the document to be queued again
    job.finished_at = datetime.now(timezone.utc)
    await session.commit()
    from case_service.hxnexus import index_queue
    # A cancelled background upload's bytes are not kept (mig 206).
    await index_queue.discard_staged(job.staged_storage_key)
    return index_queue.as_dict(job)


@router.post("/cases/{case_id}/documents/index-jobs/{job_id}/retry")
async def retry_index_job(
    case_id: uuid.UUID,
    job_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Queue a failed or cancelled job's document again, as a NEW job.

    A new row rather than resetting the old one: the failure and its
    `last_error` are the record of what went wrong, and overwriting them is how
    a document that fails every night starts looking like one that failed once.
    Same access check as enqueuing, because this IS enqueuing.
    """
    from velaris_shared.db.models import DocumentIndexJobModel
    from case_service.hxnexus import index_queue

    _check_global_rate(current_user)
    case = await _assert_case_access(case_id, current_user, session)

    job = (await session.execute(
        select(DocumentIndexJobModel).where(
            DocumentIndexJobModel.id == job_id,
            DocumentIndexJobModel.case_id == case_id,
        )
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Index job not found")
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(409, f"Job is {job.status}; only a failed or cancelled job is retried")
    if job.staged_storage_key:
        # Its bytes were deleted when the job ended — they are kept only as
        # long as the work needs them — so there is nothing left to index.
        raise HTTPException(409, "This was a background upload and the file was not kept; upload it again")

    new, created = await index_queue.enqueue_document(
        session, document_id=job.document_id, case_id=case_id,
        tenant_id=getattr(case, "tenant_id", None),
        requested_by=current_user.user_id, reindex=bool(job.reindex))
    return {**index_queue.as_dict(new), "retry_of": str(job.id),
            "already_queued": not created}


@router.get("/documents/index-jobs")
async def list_all_index_jobs(
    status: Optional[str] = Query(None, description="pending|running|done|failed|cancelled"),
    limit: int = Query(100, ge=1, le=500),
    current_user: AuthenticatedUser = Depends(hxguard.guard("ai.index_queue.read")),
    session: AsyncSession = Depends(get_session),
):
    """The whole queue for the caller's tenant — the operator's window on it.

    The per-case listing answers "what happened to this case's documents"; this
    answers "is the queue draining at all", which nobody looking at one case can
    see. Scoped exactly as `GET /cases` is: the caller's tenant plus NULL-tenant
    cases unless strict reads are on, superadmin unscoped. The scope is applied
    through the CASE, not `document_index_jobs.tenant_id`, because the case is
    the authority and a job row's copy is only what was true at enqueue.

    `drain_scheduled` says whether an ACTIVE `document_index` schedule exists for
    this tenant. Without one nothing drains the queue, and a screen that showed
    forty pending jobs without saying so would look like a slow queue rather
    than a missing one.
    """
    from sqlalchemy import func, or_

    from velaris_shared.db.models import (
        DocumentIndexJobModel, DocumentModel, ScheduleModel,
    )
    from case_service.hxnexus import index_queue
    from case_service.tenancy.identity import (
        is_superadmin, resolve_tenant, strict_case_reads,
    )

    _check_global_rate(current_user)

    scope = None
    tenant_slug = None
    if not is_superadmin(current_user):
        # A caller with no tenant claim is the platform-default tenant, never
        # "every tenant" — the rule `is_superadmin` states. The first cut read a
        # missing claim as unscoped, copied from `GET /cases`; found in the
        # 2026-09-17 audit.
        resolved = await resolve_tenant(
            session, getattr(current_user, "tenant_id", None) or "default")
        if resolved is None:
            # An unknown tenant matches nothing rather than everything.
            return {"count": 0, "counts": {}, "jobs": [], "drain_scheduled": False}
        scope, tenant_slug = resolved[0], resolved[1]

    def _scoped(q):
        q = q.join(CaseInstanceModel, CaseInstanceModel.id == DocumentIndexJobModel.case_id)
        if scope is not None:
            clause = CaseInstanceModel.tenant_id == scope
            if not strict_case_reads():
                clause = or_(clause, CaseInstanceModel.tenant_id.is_(None))
            q = q.where(clause)
        return q

    counts = {
        st: n for st, n in (await session.execute(
            _scoped(select(DocumentIndexJobModel.status, func.count()))
            .group_by(DocumentIndexJobModel.status)
        )).all()
    }

    q = _scoped(
        select(DocumentIndexJobModel, DocumentModel.filename, CaseInstanceModel.case_number)
        .outerjoin(DocumentModel, DocumentModel.id == DocumentIndexJobModel.document_id)
    ).order_by(DocumentIndexJobModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(DocumentIndexJobModel.status == status.strip().lower())
    rows = (await session.execute(q)).all()

    sched = select(func.count()).select_from(ScheduleModel).where(
        ScheduleModel.action_kind == "document_index",
        ScheduleModel.status == "active")
    if scope is not None:
        # `schedules.tenant_id` is a slug-or-id string; match both spellings.
        sched = sched.where(ScheduleModel.tenant_id.in_(
            [v for v in {str(scope), tenant_slug} if v]))
    drain_scheduled = bool((await session.execute(sched)).scalar() or 0)

    return {
        "count": len(rows),
        "counts": counts,
        "drain_scheduled": drain_scheduled,
        "jobs": [{**index_queue.as_dict(job),
                  "filename": filename or job.staged_filename,
                  "case_number": case_number}
                 for job, filename, case_number in rows],
    }


@router.post("/documents/upload")
async def upload_and_index(
    file: UploadFile = File(...),
    case_id: Optional[str] = Query(None),
    #: The SAME field, in the multipart body. Studio has always sent it there —
    #: `form.append("case_id", …)` — and the endpoint only ever read the query
    #: string, so every upload from that screen was indexed with **no case at
    #: all**: it reported "✓ Indexed N chunks" and the case's Q&A could never
    #: find one of them. Accepting both keeps every existing caller working and
    #: makes the one that was silently wrong right.
    case_id_form: Optional[str] = Form(None, alias="case_id"),
    #: Oct item 7 / mig 206. **Default False — indexed inside the request,
    #: exactly as before.** True parks the bytes and queues a job, drained by a
    #: `document_index` schedule; worth it for scans, where OCR can take
    #: `OCR_TIMEOUT_SECONDS`. Accepted in the query string or the form, like
    #: `case_id`, and requires a case: the queue's screens list jobs by case,
    #: so a case-less job would be one nobody could see.
    background: bool = Query(False),
    background_form: Optional[bool] = Form(None, alias="background"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload a document (PDF, DOCX, TXT) and index it for RAG.

    **`case_id` is checked, and it is the case that decides the tenant.**
    Until 2026-09-10 this route took a case id from the query string, indexed
    the file against it with no access check at all, and stamped the chunk with
    the *caller's* tenant. So any authenticated user could put text of their own
    choosing into another case's AI context — and a caller with no directory
    tenant wrote it with a NULL tenant, which every reader of that case treats
    as its own. That is a prompt-injection surface with an HTTP endpoint in
    front of it: the planted text is quoted back to whoever asks the case a
    question, as one of its documents.
    """
    from velaris_shared.db.models import DocumentChunkModel

    data = await file.read()
    content_type = file.content_type or "application/octet-stream"
    filename = file.filename or "upload"

    # Infer content_type from extension if browser didn't send it
    if filename.endswith(".docx"):
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif filename.endswith(".txt"):
        content_type = "text/plain"
    elif filename.endswith(".pdf"):
        content_type = "application/pdf"

    _check_global_rate(current_user)

    raw_case_id = (case_id or case_id_form or "").strip()
    try:
        cid = uuid.UUID(raw_case_id) if raw_case_id else None
    except ValueError:
        # A malformed id used to be a 500 out of `uuid.UUID`.
        raise HTTPException(400, "case_id is not a valid id")
    tenant_id = getattr(current_user, "tenant_id", None)
    if cid is not None:
        # The same gate the case-wide index route uses: tenant boundary first,
        # then the case relationship. A case the caller may not reach answers
        # 404, exactly as it does everywhere else.
        case = await _assert_case_access(cid, current_user, session)
        # And the CASE's tenant, not the caller's — a chunk is part of the
        # case's record, and stamping it with whoever happened to upload it is
        # how one tenant's row ends up inside another tenant's answers.
        tenant_id = getattr(case, "tenant_id", None)

    doc_id = uuid.uuid4()

    if background or background_form:
        if cid is None:
            raise HTTPException(400, "background upload needs a case_id")
        from case_service.hxnexus import index_queue

        # Access and the case's tenant were settled above, exactly as for the
        # synchronous path; the job row carries both because the drain has no
        # caller to ask.
        # The job row FIRST, then the bytes. The other order leaves a window —
        # a crash between the two — where the customer's file sits in storage
        # with nothing pointing at it and nothing that will ever delete it. This
        # way the worst case is a job whose bytes never arrived: the drain fails
        # it with the reason on the row, which is visible, not lost.
        key = index_queue.staged_key_for(doc_id)
        job, _created = await index_queue.enqueue_document(
            session, document_id=doc_id, case_id=cid, tenant_id=tenant_id,
            requested_by=current_user.user_id,
            staged={"storage_key": key, "filename": filename,
                    "content_type": content_type},
        )
        try:
            await get_storage_backend().put(key, data, content_type)
        except Exception as exc:
            log.warning("hxnexus: staging upload failed: %s", exc)
            await index_queue.abandon_staged(session, job.id, doc_id,
                                             "the upload could not be stored")
            raise HTTPException(503, "Could not store the upload for background indexing")
        return {"document_id": str(doc_id), "filename": filename,
                "content_type": content_type, "queued": True,
                "job": index_queue.as_dict(job),
                "note": ("Queued. Indexed by a `document_index` schedule; "
                         "without one, nothing drains the queue.")}

    report: dict = {}
    n = await index_document(
        session, doc_id, cid, data, content_type, tenant_id, report=report,
    )
    from case_service.hxnexus.service import not_embedded_note
    out = {"document_id": str(doc_id), "filename": filename,
           "content_type": content_type, "chunks_indexed": n,
           "chunks_embedded": report.get("embedded", 0)}
    if note := not_embedded_note(report):
        out["warning"] = note
    return out


# ─── Document Q&A ────────────────────────────────────────────────────

@router.post("/cases/{case_id}/qa")
async def document_qa(
    case_id: uuid.UUID,
    body: QARequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _check_global_rate(current_user)
    try:
        validate_message_length(body.question)
    except ValueError as e:
        raise HTTPException(400, str(e))
    case = await _assert_case_access(case_id, current_user, session)
    result = await qa_over_documents(
        session, case_id, body.question,
        tenant_id=getattr(case, "tenant_id", None),
        top_k=body.top_k,
        use_cloud=body.use_cloud,
        user_id=str(current_user.user_id),
    )
    return {"case_id": str(case_id), **result}


# ─── Case-scoped Q&A (ask everything on one case) ────────────────────

class AskRequest(BaseModel):
    question: str


@router.post("/cases/{case_id}/ask")
async def case_ask(
    case_id: uuid.UUID,
    body: AskRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Ask a question against the WHOLE case — variables, timeline, messages,
    document text, verifications, and (permission-gated) sealed transcripts.

    Sovereignty is a per-tenant choice: `tenant.settings["ai"].egress` is
    `local_only` (default — forced local Ollama) or `external_allowed`
    (tenant consented to external processing; egress is pseudonymized,
    minimized, and audited). Feature itself is opt-in via `ai.case_qa`."""
    from velaris_shared import hxguard
    from case_service.hxnexus.case_qa import ask_case

    _check_global_rate(current_user)
    try:
        validate_message_length(body.question)
    except ValueError as e:
        raise HTTPException(400, str(e))

    case = await session.get(CaseInstanceModel, case_id)
    if case is None or not await same_tenant(session, case.tenant_id, current_user.tenant_id):
        raise coded(404, LLM_NOTFOUND_404, "Case not found")
    await hxguard.require_case(session, current_user, "cases.ask", case_id)

    # Transcript feed rides the same gate as viewing the recording.
    try:
        await hxguard.require_case(session, current_user, "meet.recording.view", case_id)
        can_view_transcripts = True
    except HTTPException:
        can_view_transcripts = False

    try:
        result = await ask_case(session, case=case, question=body.question,
                                user=current_user,
                                can_view_transcripts=can_view_transcripts)
    except ProcessingRefused as e:
        # 403, not 400: the request is well-formed and the caller authorised.
        # The platform is refusing on legal grounds, which is a different thing
        # from the caller having got something wrong.
        raise coded(
            403, CONSENT_REFUSED_403,
            f"Refused: the subject of this case has no lawful basis for "
            f"{e.purpose!r} ({e.reason}).",
        )
    except AgentRefused as e:
        # 403, not 400. The request was well-formed and the caller authorised;
        # HxArbiter's `agent` namespace declined. Ordered before the
        # PermissionError arm below because it subclasses it — reversing these
        # two lines silently turns every policy refusal back into a 400.
        raise HTTPException(403, str(e))
    except PermissionError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"case_id": str(case_id), **result}


# ─── Chat ─────────────────────────────────────────────────────────────

@router.post("/chat")
async def hxnexus_chat(
    body: ChatRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # Rate limit: 20 chat requests per user per minute
    allowed, retry = chat_rate_limiter.is_allowed(str(current_user.user_id))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Chat rate limit reached. Try again in {retry}s.",
            headers={"Retry-After": str(retry)},
        )

    # Input length cap
    try:
        validate_message_length(body.message)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Log scan signals (informational — does not block)
    scan = scan_input(body.message)
    if scan.flagged:
        log.warning(
            "hxnexus:router: flagged input user=%s signals=%s",
            current_user.user_id, scan.signals,
        )

    # Case-attached chat gets the case's assembled context — same gatherer
    # and permission model as /cases/{id}/ask (transcripts only with
    # meet.recording.view; withheld sources are disclosed to the model).
    case_context = None
    if body.case_id:
        from velaris_shared import hxguard
        from case_service.hxnexus.case_qa import _gather_sources

        case = await _assert_case_access(body.case_id, current_user, session)
        try:
            await hxguard.require_case(session, current_user, "meet.recording.view", body.case_id)
            can_view_transcripts = True
        except HTTPException:
            can_view_transcripts = False
        try:
            sources, withheld = await _gather_sources(
                session, case, current_user, can_view_transcripts=can_view_transcripts)
            case_context = "\n\n".join(f"[{s['sid']}] {s['label']}\n{s['text']}" for s in sources)
            if withheld:
                case_context += f"\n\nWithheld (no permission): {'; '.join(withheld)}"
        except Exception as exc:
            log.warning("chat case-context gathering failed for %s: %s", body.case_id, exc)

    result = await chat(
        session,
        conversation_id=body.conversation_id,
        user_id=current_user.user_id,
        case_id=body.case_id,
        message=body.message,
        tenant_id=getattr(current_user, "tenant_id", None),
        use_cloud=body.use_cloud,
        case_context=case_context,
    )
    return result


# ─── Conversations ────────────────────────────────────────────────────

@router.get("/conversations")
async def list_conversations(
    case_id: Optional[uuid.UUID] = Query(None),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    q = select(CopilotConversationModel).where(
        CopilotConversationModel.user_id == current_user.user_id
    ).order_by(CopilotConversationModel.updated_at.desc())
    if case_id:
        q = q.where(CopilotConversationModel.case_id == case_id)
    rows = (await session.execute(q)).scalars().all()
    return [{"id": str(c.id), "case_id": str(c.case_id) if c.case_id else None,
             "created_at": c.created_at, "updated_at": c.updated_at} for c in rows]


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(
    conversation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(CopilotConversationModel, conversation_id)
    if not conv or conv.user_id != current_user.user_id:
        raise coded(404, LLM_NOTFOUND_404, "Conversation not found")
    msgs = (await session.execute(
        select(CopilotMessageModel)
        .where(CopilotMessageModel.conversation_id == conversation_id)
        .order_by(CopilotMessageModel.created_at)
    )).scalars().all()
    return [{"role": m.role, "content": m.content, "created_at": m.created_at} for m in msgs]


@router.get("/conversations/{conversation_id}/transcript", response_class=PlainTextResponse)
async def get_transcript(
    conversation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Download a plain-text transcript of the conversation."""
    conv = await session.get(CopilotConversationModel, conversation_id)
    if not conv or conv.user_id != current_user.user_id:
        raise coded(404, LLM_NOTFOUND_404, "Conversation not found")

    msgs = (await session.execute(
        select(CopilotMessageModel)
        .where(CopilotMessageModel.conversation_id == conversation_id)
        .order_by(CopilotMessageModel.created_at)
    )).scalars().all()

    lines = [
        f"HxNexus Conversation Transcript",
        f"Conversation ID : {conversation_id}",
        f"Case ID         : {conv.case_id or 'N/A'}",
        f"Started         : {conv.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"{'─' * 60}",
        "",
    ]
    for m in msgs:
        speaker = "User" if m.role == "user" else "HxNexus"
        ts = m.created_at.strftime("%H:%M:%S")
        lines.append(f"[{ts}] {speaker}:")
        lines.append(m.content)
        lines.append("")

    return "\n".join(lines)


@router.post("/conversations/{conversation_id}/summarize")
async def summarize_conversation(
    conversation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Use HxNexus to generate a structured summary of the conversation."""
    conv = await session.get(CopilotConversationModel, conversation_id)
    if not conv or conv.user_id != current_user.user_id:
        raise coded(404, LLM_NOTFOUND_404, "Conversation not found")

    msgs = (await session.execute(
        select(CopilotMessageModel)
        .where(CopilotMessageModel.conversation_id == conversation_id)
        .order_by(CopilotMessageModel.created_at)
    )).scalars().all()

    if not msgs:
        return {"summary": "No messages in this conversation.", "key_points": [], "action_items": []}

    transcript = "\n".join(
        f"{'User' if m.role == 'user' else 'HxNexus'}: {m.content}" for m in msgs
    )

    llm = get_llm_backend()
    is_available = await llm.check_available() if hasattr(llm, "check_available") else llm.available
    if not is_available:
        return {"summary": "LLM unavailable — cannot generate summary.", "key_points": [], "action_items": []}

    system = """You are HxNexus. Summarize the following conversation.
Return JSON: {"summary": "...", "key_points": ["...", "..."], "action_items": ["..."]}"""

    import json
    raw = await llm.complete(f"Conversation:\n{transcript}", system=system, temperature=0.3)
    try:
        result = json.loads(raw)
        return {
            "conversation_id": str(conversation_id),
            "summary": result.get("summary", ""),
            "key_points": result.get("key_points", []),
            "action_items": result.get("action_items", []),
        }
    except Exception:
        return {"conversation_id": str(conversation_id), "summary": raw,
                "key_points": [], "action_items": []}


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(CopilotConversationModel, conversation_id)
    if not conv or conv.user_id != current_user.user_id:
        raise coded(404, LLM_NOTFOUND_404, "Conversation not found")
    # What was asked of the AI about a held case is part of its record (Oct item 13).
    from case_service.legal_hold import service as _legal_hold
    await _legal_hold.require_not_held(
        session, getattr(conv, "case_id", None), "changed by deleting a conversation about it")
    await session.delete(conv)
    await session.commit()


# ─── P42: Polyglot Intelligence ──────────────────────────────────────────────

from case_service.hxnexus.polyglot import translate, analyze, compare
from case_service.hxnexus.autodoc import get_business_guide, get_dev_guide
from velaris_shared.db.models import BpmConceptModel
from velaris_shared.core.error_codes import (
    CONSENT_REFUSED_403,
    LLM_NOTFOUND_404,
    LLM_NOT_ASSIGNED_403,
    coded,
)
from case_service.compliance.consent import ProcessingRefused


class TranslateRequest(BaseModel):
    tool: str
    concept: str


class AnalyzeRequest(BaseModel):
    tool: str
    text: str


class CompareRequest(BaseModel):
    tool: str
    question: str


@router.get("/polyglot/concepts")
async def list_bpm_concepts(
    tool: Optional[str] = Query(None, description="Filter by tool: pega|camunda|appian|servicenow"),
    confidence: Optional[str] = Query(None, description="Filter by confidence: exact|close|partial|manual"),
    q: Optional[str] = Query(None, description="Search by concept name"),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """List all BPM concept mappings from the knowledge base."""
    stmt = select(BpmConceptModel)
    if tool:
        stmt = stmt.where(BpmConceptModel.source_tool == tool.lower())
    if confidence:
        stmt = stmt.where(BpmConceptModel.confidence == confidence)
    if q:
        stmt = stmt.where(BpmConceptModel.source_concept.ilike(f"%{q}%"))
    rows = (await session.execute(stmt.order_by(BpmConceptModel.source_tool, BpmConceptModel.source_concept))).scalars().all()
    return {
        "total": len(rows),
        "concepts": [
            {
                "id": str(r.id), "source_tool": r.source_tool,
                "source_concept": r.source_concept, "helix_equiv": r.helix_equiv,
                "helix_node_type": r.helix_node_type, "description": r.description,
                "example": r.example, "confidence": r.confidence, "notes": r.notes,
            }
            for r in rows
        ],
    }


@router.post("/polyglot/translate")
async def translate_concept(
    body: TranslateRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Translate a BPM tool concept to its Velaris equivalent."""
    return await translate(body.tool, body.concept, session)


@router.post("/polyglot/analyze")
async def analyze_fragment(
    body: AnalyzeRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Analyse a raw BPM config fragment and map it to Velaris constructs."""
    return await analyze(body.tool, body.text, session)


@router.post("/polyglot/compare")
async def compare_approaches(
    body: CompareRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Compare how a BPM tool does something vs how Velaris does it."""
    return await compare(body.tool, body.question, session)


@router.get("/docs/business", response_class=PlainTextResponse)
async def business_guide(
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """AI-generated plain-English business guide for this platform."""
    content = await get_business_guide(session, force=force)
    return PlainTextResponse(content=content, media_type="text/markdown")


@router.get("/docs/developer", response_class=PlainTextResponse)
async def developer_guide(
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """AI-generated technical developer guide for this platform."""
    content = await get_dev_guide(session, force=force)
    return PlainTextResponse(content=content, media_type="text/markdown")


@router.post("/docs/regenerate")
async def regenerate_docs(
    session: AsyncSession = Depends(get_session),
    current_user: AuthenticatedUser = Depends(hxguard.guard("docs.guides.regenerate")),
):
    """Force regeneration of both business and developer guides."""
    allowed, retry = regen_rate_limiter.is_allowed(str(current_user.user_id))
    if not allowed:
        raise HTTPException(
            429, f"Regeneration rate limit reached. Try again in {retry}s.",
            headers={"Retry-After": str(retry)},
        )
    business = await get_business_guide(session, force=True)
    dev = await get_dev_guide(session, force=True)
    return {
        "business_guide_chars": len(business),
        "dev_guide_chars": len(dev),
        "status": "regenerated",
    }
