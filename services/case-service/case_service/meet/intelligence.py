"""HxMeet P4a — local-only session intelligence.

Pipeline (nothing ever leaves the infrastructure):
  sealed recording  >  unseal in memory (tenant DEK)  >  faster-whisper
  transcript (CPU, thread executor)  >  transcript stored as a case document
  >  HxNexus (local Ollama) summary + action items  >  results row.

Fail-closed at every seam: tenant opt-in default OFF, faster-whisper not
installed = 501, AI unavailable = transcript still lands, summary is skipped
and says so. Model versions are stored with every run so old results stay
interpretable after upgrades.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.config import get_settings
from case_service.db.models import (
    CaseSessionIntelligenceModel,
    CaseSessionModel,
    TenantModel,
)

log = logging.getLogger(__name__)

_SUMMARY_SYSTEM = (
    "You summarize a transcribed customer-facing case session. Be factual and "
    "concise. Never invent statements that are not in the transcript."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


async def tenant_intelligence_enabled(session: AsyncSession, tenant_slug: str | None) -> bool:
    """Per-tenant opt-in, default OFF — analysis of recorded conversations is
    an egress-policy-level decision even when it runs locally."""
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (tenant_slug or "default"))
    )).scalars().first()
    return bool(((tenant.settings or {}).get("meet", {}) if tenant else {}).get("intelligence", False))


async def _unseal_recording(session: AsyncSession, row: CaseSessionModel) -> bytes:
    from case_service.documents.service import DocumentService
    from case_service.hxvault import crypto as vault
    from case_service.hxvault.keyring import ensure_dek
    from case_service.meet import service as meet

    sealed, _name, _ct = await DocumentService().download(session, row.recording_document_id)
    tenant_row = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (row.tenant_id or "default"))
    )).scalars().first()
    dek = await ensure_dek(session, tenant_row.id if tenant_row else None)
    return vault.open_(dek, sealed, f"{meet.RECORDING_AAD_PREFIX}{row.id}".encode())


def _transcribe(mp4_bytes: bytes, model_size: str) -> tuple[str, str | None, int, str]:
    """Blocking CPU work — run via asyncio.to_thread. The plaintext video
    stays in memory (BytesIO); nothing is written to disk."""
    from faster_whisper import WhisperModel
    import faster_whisper as fw

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(io.BytesIO(mp4_bytes), vad_filter=True)
    lines = []
    for seg in segments:
        stamp = f"[{int(seg.start // 60):02d}:{int(seg.start % 60):02d}]"
        lines.append(f"{stamp} {seg.text.strip()}")
    transcript = "\n".join(lines)
    return transcript, info.language, int(info.duration or 0), getattr(fw, "__version__", "unknown")


async def _summarize(transcript: str) -> tuple[str | None, list, str | None]:
    """Local LLM summary — skipped honestly when AI is unavailable."""
    from case_service.hxnexus.factory import check_ai_available, get_llm_backend

    if not await check_ai_available():
        return None, [], None
    llm = get_llm_backend()
    clipped = transcript[:24_000]  # keep well inside local context windows
    try:
        summary = await llm.complete(
            f"Transcript of a case session:\n\n{clipped}\n\n"
            "Write a 3-6 sentence factual summary of what was discussed and decided.",
            system=_SUMMARY_SYSTEM, temperature=0.2)
        raw = await llm.complete(
            f"Transcript of a case session:\n\n{clipped}\n\n"
            'List concrete action items as a JSON array of strings (e.g. '
            '["Send the policy document"]). Return ONLY the JSON array; return '
            "[] if there are none.",
            system=_SUMMARY_SYSTEM, temperature=0.1)
        try:
            items = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
            if not isinstance(items, list):
                items = []
            items = [str(i)[:500] for i in items][:20]
        except Exception:
            items = []
        return (summary or "").strip() or None, items, getattr(llm, "model", None) or "hxnexus"
    except Exception as exc:
        log.warning("session summary failed: %s", exc)
        return None, [], None


async def run_intelligence_job(session_id: uuid.UUID) -> None:
    """Background job — owns its own DB session (the request one is gone)."""
    from case_service.db.session import get_session_factory
    from case_service.documents.service import DocumentService
    from case_service.api.routers.cases import _audit

    factory = get_session_factory()
    async with factory() as session:
        intel = await session.get(CaseSessionIntelligenceModel, session_id)
        row = await session.get(CaseSessionModel, session_id)
        if intel is None or row is None:
            return
        intel.status = "running"
        await session.commit()

        try:
            # P4a-live-2: a sealed live transcript makes re-transcription
            # redundant — unseal it as the analysis input and point at the
            # SEALED document (no plaintext copy is ever stored beside it).
            if row.transcript_status == "sealed" and row.transcript_document_id:
                from case_service.meet import service as meet_svc
                from case_service.hxvault import crypto as vault
                from case_service.hxvault.keyring import ensure_dek

                sealed, _n, _ct = await DocumentService().download(
                    session, row.transcript_document_id)
                tenant_row = (await session.execute(
                    select(TenantModel).where(TenantModel.slug == (row.tenant_id or "default"))
                )).scalars().first()
                dek = await ensure_dek(session, tenant_row.id if tenant_row else None)
                transcript = vault.open_(
                    dek, sealed, f"{meet_svc.TRANSCRIPT_AAD_PREFIX}{row.id}".encode()).decode()
                language, duration = None, None
                doc_id = row.transcript_document_id
                asr_version = "live-captions (sealed transcript reused)"
            else:
                plaintext = await _unseal_recording(session, row)
                model_size = get_settings().meet_whisper_model
                transcript, language, duration, fw_version = await asyncio.to_thread(
                    _transcribe, plaintext, model_size)
                del plaintext

                doc = await DocumentService().upload(
                    session, case_id=row.case_id,
                    filename=f"session-{row.id}-transcript.txt",
                    data=transcript.encode(),
                    content_type="text/plain",
                    uploaded_by="hxmeet-intelligence",
                    tenant_id=row.tenant_id,
                )
                doc_id = doc.id
                asr_version = f"faster-whisper/{fw_version}/{model_size}"

            summary, action_items, llm_model = await _summarize(transcript)

            intel.status = "completed"
            intel.transcript_document_id = doc_id
            intel.summary = summary
            intel.action_items = action_items
            intel.language = language
            intel.duration_seconds = duration
            intel.model_versions = {
                "whisper": asr_version,
                **({"llm": llm_model} if llm_model else {"llm": "unavailable — summary skipped"}),
            }
            intel.error = None
            intel.completed_at = _utcnow()
            await _audit(session, row.case_id, "session_analyzed",
                         actor_id=intel.requested_by,
                         details={"session_id": str(row.id),
                                  "transcript_document_id": str(doc_id),
                                  "summary": summary is not None})
            await session.commit()
        except Exception as exc:
            log.warning("session intelligence failed for %s: %s", session_id, exc)
            await session.rollback()
            intel = await session.get(CaseSessionIntelligenceModel, session_id)
            if intel is not None:
                intel.status = "failed"
                intel.error = str(exc)[:500]
                intel.completed_at = _utcnow()
                await session.commit()


def intelligence_view(intel: CaseSessionIntelligenceModel | None) -> dict:
    if intel is None:
        return {"status": "none"}
    return {
        "status":                 intel.status,
        "transcript_document_id": str(intel.transcript_document_id) if intel.transcript_document_id else None,
        "summary":                intel.summary,
        "action_items":           intel.action_items or [],
        "language":               intel.language,
        "duration_seconds":       intel.duration_seconds,
        "model_versions":         intel.model_versions or {},
        "error":                  intel.error,
        "requested_by":           intel.requested_by,
        "created_at":             intel.created_at.isoformat() if intel.created_at else None,
        "completed_at":           intel.completed_at.isoformat() if intel.completed_at else None,
    }
