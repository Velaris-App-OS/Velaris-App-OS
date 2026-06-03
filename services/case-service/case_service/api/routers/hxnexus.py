"""HxNexus AI Copilot API — P30.

Endpoints:
  GET    /hxnexus/status                              backend health + model info
  POST   /hxnexus/cases/{id}/suggest                  next-best-action
  POST   /hxnexus/cases/{id}/documents/index          index case documents
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

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    CaseInstanceModel, DocumentModel, DocumentVersionModel,
    CopilotConversationModel, CopilotMessageModel,
    CaseAssignmentModel,
)
from case_service.db.session import get_session
from case_service.hxnexus.factory import get_llm_backend, get_ai_info, check_ai_available
from case_service.hxnexus.guard import (
    chat_rate_limiter, regen_rate_limiter, global_rate_limiter,
    validate_message_length, scan_input,
)
from case_service.hxnexus.service import (
    next_best_action, qa_over_documents, chat, index_document,
)
from case_service.storage import get_storage_backend

log = logging.getLogger(__name__)

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
    case = await session.get(CaseInstanceModel, case_id)
    if not case:
        raise HTTPException(404, "Case not found")

    # Admins and managers have platform-wide case access
    if user.is_admin or user.has_role("manager"):
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
        raise HTTPException(403, "You are not assigned to this case.")

    return case


# ─── Pydantic schemas ────────────────────────────────────────────────

class QARequest(BaseModel):
    question: str
    top_k: int = 5


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[uuid.UUID] = None
    case_id: Optional[uuid.UUID] = None


class IndexRequest(BaseModel):
    reindex: bool = False


# ─── Status (real async liveness check) ──────────────────────────────

@router.get("/status")
async def hxnexus_status(_: AuthenticatedUser = Depends(get_current_user)):
    """Return HxNexus availability and active backend info."""
    available = await check_ai_available()
    info = get_ai_info()
    return {
        "name": "HxNexus",
        "backend": info["backend"],
        "available": available,
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
    case_data = {
        "id": str(case.id), "status": case.status,
        "priority": case.priority, "data": case.data or {},
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
    from case_service.db.models import DocumentChunkModel

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

    storage = get_storage_backend()
    total_chunks = 0
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
        n = await index_document(
            session, doc.id, case_id, data,
            doc.content_type, getattr(case, "tenant_id", None),
        )
        total_chunks += n
        results.append({"document_id": str(doc.id), "filename": doc.filename, "chunks": n})

    return {"case_id": str(case_id), "documents_indexed": len(results),
            "total_chunks": total_chunks, "details": results}


@router.post("/documents/upload")
async def upload_and_index(
    file: UploadFile = File(...),
    case_id: Optional[str] = Query(None),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload a document (PDF, DOCX, TXT) and immediately index it for RAG."""
    from case_service.db.models import DocumentChunkModel

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

    cid = uuid.UUID(case_id) if case_id else None
    doc_id = uuid.uuid4()
    n = await index_document(
        session, doc_id, cid, data, content_type,
        getattr(current_user, "tenant_id", None),
    )
    return {"document_id": str(doc_id), "filename": filename,
            "content_type": content_type, "chunks_indexed": n}


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
    )
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

    result = await chat(
        session,
        conversation_id=body.conversation_id,
        user_id=current_user.user_id,
        case_id=body.case_id,
        message=body.message,
        tenant_id=getattr(current_user, "tenant_id", None),
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
        raise HTTPException(404, "Conversation not found")
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
        raise HTTPException(404, "Conversation not found")

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
        raise HTTPException(404, "Conversation not found")

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
        raise HTTPException(404, "Conversation not found")
    await session.delete(conv)
    await session.commit()


# ─── P42: Polyglot Intelligence ──────────────────────────────────────────────

from case_service.hxnexus.polyglot import translate, analyze, compare
from case_service.hxnexus.autodoc import get_business_guide, get_dev_guide
from case_service.db.models import BpmConceptModel


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
    """Translate a BPM tool concept to its Helix equivalent."""
    return await translate(body.tool, body.concept, session)


@router.post("/polyglot/analyze")
async def analyze_fragment(
    body: AnalyzeRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Analyse a raw BPM config fragment and map it to Helix constructs."""
    return await analyze(body.tool, body.text, session)


@router.post("/polyglot/compare")
async def compare_approaches(
    body: CompareRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Compare how a BPM tool does something vs how Helix does it."""
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
    current_user: AuthenticatedUser = Depends(require_role("designer", "admin")),
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
