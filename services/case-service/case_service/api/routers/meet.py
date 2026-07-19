"""HxMeet API — real-time case sessions (P1 off-platform, P2 embedded).

Starting/ending a session is case work (``meet.start`` via the case-level
PDP); joining an embedded session is ``meet.join``; reading sessions inherits
the case's view authorization (``case.read``). 404 anti-oracle throughout —
an unauthorized case id looks nonexistent, and the public guest-token
exchange returns a uniform 404 for every failure mode.

Two unauthenticated endpoints, both narrow: the guest token exchange
(single-use invite, IP rate-limited) and the LiveKit webhook (signature-
verified against the API secret).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import hxguard
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.config import get_settings
from case_service.db import repository as repo
from case_service.db.models import CaseSessionModel
from case_service.db.session import get_session
from case_service.hxnexus.guard import _RateLimiter
from case_service.meet import livekit
from case_service.meet import service as meet

router = APIRouter(prefix="/meet", tags=["meet"])


def _tenant(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


async def _authorized_case(session, user, case_id: str, action: str):
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(404, "Case not found")
    case = await repo.get_case_instance(session, cid)
    if case is None or (case.tenant_id is not None and str(case.tenant_id) != _tenant(user)):
        raise HTTPException(404, "Case not found")
    await hxguard.require_case(session, user, action, cid)
    return case


def _session_view(s: CaseSessionModel) -> dict:
    return {
        "id":                  str(s.id),
        "case_id":             str(s.case_id),
        "driver":              s.driver,
        "provider":            s.provider,
        "status":              s.status,
        "title":               s.title,
        "join_url":            s.join_url,
        "external_meeting_id": s.external_meeting_id,
        "started_by":          s.started_by,
        "started_at":          s.started_at.isoformat() if s.started_at else None,
        "ended_at":            s.ended_at.isoformat() if s.ended_at else None,
        "record_intent":       s.record_intent,
        "recording_status":    s.recording_status,
        "recording_document_id": str(s.recording_document_id) if s.recording_document_id else None,
        "transcript_status":   s.transcript_status,
        "transcript_document_id": str(s.transcript_document_id) if s.transcript_document_id else None,
        "created_at":          s.created_at.isoformat() if s.created_at else None,
    }


class StartSessionBody(BaseModel):
    title: str | None = None
    provider: str | None = None       # teams | zoom | gmeet | generic (else tenant default)
    connector_id: str | None = None   # explicit connector override
    record: bool = False              # P3: declare recording intent (embedded only)


@router.get("/providers")
async def providers(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Enabled meeting connectors for this tenant + the active driver."""
    engine, device = meet_asr.detect_backend()
    return {
        "driver":             await meet.resolve_driver(session, _tenant(user)),
        "embedded_available": livekit.configured(),
        "providers":          await meet.list_providers(session, _tenant(user)),
        # P4a-live: what the auto-probe picked — operators see the active path.
        "live_captions": {
            "enabled":       await tenant_live_captions_enabled(session, _tenant(user)),
            "asr_installed": meet_asr.available(),
            "engine":        engine,
            "device":        device,
        },
    }


@router.post("/cases/{case_id}/sessions", status_code=201)
async def start_session(
    case_id: str,
    body: StartSessionBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    case = await _authorized_case(session, user, case_id, "meet.start")

    driver = await meet.resolve_driver(session, _tenant(user))
    if driver == "embedded":
        if not livekit.configured():
            raise HTTPException(501, "Embedded driver is not configured (LiveKit url/key/secret)")
        if body.record and not meet.recording_available():
            raise HTTPException(501, "Recording is not configured (egress recordings dir)")
        row = await meet.start_embedded_session(
            session, case_id=case.id, case_type_id=case.case_type_id,
            tenant_id=_tenant(user), started_by=user.user_id, title=body.title,
            record_intent=body.record,
        )
        return _session_view(row)
    if body.record:
        raise HTTPException(400, "Recording intent applies to the embedded driver only")
    if driver != "off_platform":
        raise HTTPException(501, f"Session driver '{driver}' is not available yet")

    connector_id = None
    if body.connector_id:
        try:
            connector_id = uuid.UUID(body.connector_id)
        except ValueError:
            raise HTTPException(422, "Invalid connector_id")

    try:
        row = await meet.start_session(
            session,
            case_id=case.id,
            case_type_id=case.case_type_id,
            tenant_id=_tenant(user),
            started_by=user.user_id,
            title=body.title,
            provider=body.provider,
            connector_id=connector_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, f"Meeting provider error: {exc}")
    return _session_view(row)


@router.get("/cases/{case_id}/sessions")
async def list_sessions(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    case = await _authorized_case(session, user, case_id, "case.read")
    return {"sessions": [_session_view(s) for s in await meet.list_sessions(session, case.id)]}


@router.post("/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    cancelled: bool = False,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(404, "Session not found")
    row = (await session.execute(
        select(CaseSessionModel).where(CaseSessionModel.id == sid)
    )).scalar_one_or_none()
    if row is None or (row.tenant_id is not None and row.tenant_id != _tenant(user)):
        raise HTTPException(404, "Session not found")
    case = await _authorized_case(session, user, str(row.case_id), "meet.start")
    if row.status in ("ended", "cancelled"):
        return _session_view(row)
    return _session_view(await meet.end_session(
        session, row=row, case_type_id=case.case_type_id, actor=user.user_id, cancelled=cancelled,
    ))


# ── P2: embedded driver (LiveKit) ─────────────────────────────────────────────

async def _visible_session(
    session: AsyncSession, user: AuthenticatedUser, session_id: str,
) -> CaseSessionModel:
    """Tenant-scoped session lookup, 404 anti-oracle."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(404, "Session not found")
    row = (await session.execute(
        select(CaseSessionModel).where(CaseSessionModel.id == sid)
    )).scalar_one_or_none()
    if row is None or (row.tenant_id is not None and row.tenant_id != _tenant(user)):
        raise HTTPException(404, "Session not found")
    return row


def _require_active_embedded(row: CaseSessionModel) -> None:
    if row.driver != "embedded":
        raise HTTPException(400, "Not an embedded session")
    if row.status != "active":
        raise HTTPException(409, "Session is not active")
    if not livekit.configured():
        raise HTTPException(501, "Embedded driver is not configured (LiveKit url/key/secret)")


@router.post("/sessions/{session_id}/token")
async def session_token(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Room token for a worker (`meet.join`). The LiveKit API secret never
    leaves the server — this returns a minted, room-scoped, expiring token."""
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.join")
    _require_active_embedded(row)
    return await meet.mint_worker_token(
        session, row=row, user_id=user.user_id,
        display_name=user.username or user.email or user.user_id,
    )


class InviteBody(BaseModel):
    customer_id: str | None = None   # portal customer (preferred)
    email: str | None = None         # bare-email guest (fallback principal)
    display_name: str | None = None


@router.post("/sessions/{session_id}/invites", status_code=201)
async def invite_guest(
    session_id: str,
    body: InviteBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Invite an external guest (`meet.start` — inviting is hosting work).
    Returns the raw single-use token exactly once; only its hash is stored."""
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.start")
    _require_active_embedded(row)

    customer_id = None
    if body.customer_id:
        try:
            customer_id = uuid.UUID(body.customer_id)
        except ValueError:
            raise HTTPException(422, "Invalid customer_id")
    try:
        participant, raw_token = await meet.create_guest_invite(
            session, row=row, invited_by=user.user_id,
            customer_id=customer_id, email=body.email, display_name=body.display_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "participant_id": str(participant.id),
        "identity":       participant.identity,
        "invite_token":   raw_token,
        "join_path":      f"/meet/join?token={raw_token}",
        "expires_at":     participant.invite_expires_at.isoformat(),
    }


@router.get("/sessions/{session_id}/participants")
async def participants(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "case.read")
    return {"participants": [
        {
            "id":           str(p.id),
            "identity":     p.identity,
            "display_name": p.display_name,
            "role":         p.role,
            "invited_by":   p.invited_by,
            "joined_at":    p.joined_at.isoformat() if p.joined_at else None,
            "left_at":      p.left_at.isoformat() if p.left_at else None,
        }
        for p in await meet.list_participants(session, row.id)
    ]}


# Public: single-use invite → room token. Uniform 404 on every failure mode
# (no oracle for token guessing), IP rate-limited.
_guest_rate = _RateLimiter(max_calls=10, window_seconds=60)


class GuestTokenBody(BaseModel):
    invite_token: str


@router.post("/guest/token")
async def guest_token(
    body: GuestTokenBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    allowed, _retry = _guest_rate.is_allowed(request.client.host if request.client else "unknown")
    if not allowed:
        raise HTTPException(429, "Too many attempts")
    try:
        return await meet.exchange_guest_invite(session, body.invite_token)
    except ValueError:
        raise HTTPException(404, "Invite not found")


# ── P3: sealed recording ─────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/recording/start")
async def recording_start(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Start the room egress (`meet.start`). Hard-gated on record_intent and
    per-participant consent — 409 with the missing identities otherwise."""
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.start")
    _require_active_embedded(row)
    if not meet.recording_available():
        raise HTTPException(501, "Recording is not configured (egress recordings dir)")
    try:
        return _session_view(await meet.start_recording(session, row=row, actor=user.user_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, f"Egress error: {exc}")


@router.post("/sessions/{session_id}/recording/stop")
async def recording_stop(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.start")
    try:
        return _session_view(await meet.stop_recording(session, row=row, actor=user.user_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, f"Egress error: {exc}")


async def _sealed_recording(session, user, session_id: str):
    """Common gate for download/verify: meet.recording.view + a sealed doc."""
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.recording.view")
    if row.recording_status != "sealed" or row.recording_document_id is None:
        raise HTTPException(404, "No sealed recording for this session")
    return row


@router.get("/sessions/{session_id}/recording")
async def recording_download(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Stream the recording (`meet.recording.view`) — unsealed server-side
    with the tenant DEK; the stored document is ciphertext."""
    from fastapi.responses import Response

    from case_service.documents.service import DocumentService
    from case_service.hxvault import crypto as vault
    from case_service.hxvault.keyring import ensure_dek
    from case_service.db.models import TenantModel
    from sqlalchemy import select as _select

    row = await _sealed_recording(session, user, session_id)
    sealed, _name, _ct = await DocumentService().download(session, row.recording_document_id)
    tenant_row = (await session.execute(
        _select(TenantModel).where(TenantModel.slug == (row.tenant_id or "default"))
    )).scalars().first()
    dek = await ensure_dek(session, tenant_row.id if tenant_row else None)
    try:
        plaintext = vault.open_(dek, sealed, f"{meet.RECORDING_AAD_PREFIX}{row.id}".encode())
    except Exception:
        raise HTTPException(409, "Recording unsealing failed (tampered or key mismatch)")

    # GDPR access log: every view of a sealed recording is a case audit event.
    from case_service.api.routers.cases import _audit
    await _audit(session, row.case_id, "recording_viewed", actor_id=user.user_id,
                 details={"session_id": str(row.id)})
    await session.commit()

    # `inline`, not `attachment`: Studio streams this into an in-app player —
    # no plaintext MP4 should land in a Downloads folder by default.
    return Response(content=plaintext, media_type="video/mp4", headers={
        "Content-Disposition": f'inline; filename="session-{row.id}.mp4"',
    })


@router.get("/sessions/{session_id}/recording/verify")
async def recording_verify(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Recompute the recording hash and compare with the audit-chain seal —
    an offline-style attestation that the recording is unaltered."""
    import hashlib as _hashlib

    from case_service.documents.service import DocumentService
    from case_service.hxvault import crypto as vault
    from case_service.hxvault.keyring import ensure_dek
    from case_service.db.models import TenantModel
    from sqlalchemy import select as _select

    row = await _sealed_recording(session, user, session_id)
    sealed, _name, _ct = await DocumentService().download(session, row.recording_document_id)
    tenant_row = (await session.execute(
        _select(TenantModel).where(TenantModel.slug == (row.tenant_id or "default"))
    )).scalars().first()
    dek = await ensure_dek(session, tenant_row.id if tenant_row else None)
    try:
        plaintext = vault.open_(dek, sealed, f"{meet.RECORDING_AAD_PREFIX}{row.id}".encode())
    except Exception:
        return {"verified": False, "reason": "unseal_failed (tampered or key mismatch)"}
    actual = _hashlib.sha256(plaintext).hexdigest()
    ref = row.audit_anchor_ref or ""
    sealed_hash = next((p.split("sha256:", 1)[1] for p in ref.split(";") if p.startswith("sha256:")), None)
    return {
        "verified":     sealed_hash == actual,
        "sha256":       actual,
        "sealed_sha256": sealed_hash,
        "anchor_ref":   ref,
    }


# ── P4a-live-2: sealed live transcript (the recording's twin) ────────────────

async def _sealed_transcript(session, user, session_id: str):
    """Same gate as the recording: meet.recording.view + a sealed doc,
    404 anti-oracle on everything else."""
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.recording.view")
    if row.transcript_status != "sealed" or row.transcript_document_id is None:
        raise HTTPException(404, "No sealed transcript for this session")
    return row


async def _unseal_transcript(session, row) -> bytes:
    from case_service.documents.service import DocumentService
    from case_service.hxvault import crypto as vault
    from case_service.hxvault.keyring import ensure_dek
    from case_service.db.models import TenantModel
    from sqlalchemy import select as _select

    sealed, _name, _ct = await DocumentService().download(session, row.transcript_document_id)
    tenant_row = (await session.execute(
        _select(TenantModel).where(TenantModel.slug == (row.tenant_id or "default"))
    )).scalars().first()
    dek = await ensure_dek(session, tenant_row.id if tenant_row else None)
    return vault.open_(dek, sealed, f"{meet.TRANSCRIPT_AAD_PREFIX}{row.id}".encode())


@router.get("/sessions/{session_id}/transcript")
async def transcript_view(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Stream the sealed live transcript (`meet.recording.view`) — unsealed
    server-side, inline text, every view audited (GDPR access log, exactly
    like recording_viewed)."""
    from fastapi.responses import Response

    row = await _sealed_transcript(session, user, session_id)
    try:
        plaintext = await _unseal_transcript(session, row)
    except Exception:
        raise HTTPException(409, "Transcript unsealing failed (tampered or key mismatch)")

    from case_service.api.routers.cases import _audit
    await _audit(session, row.case_id, "transcript_viewed", actor_id=user.user_id,
                 details={"session_id": str(row.id)})
    await session.commit()

    return Response(content=plaintext, media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": f'inline; filename="session-{row.id}-transcript.txt"',
    })


@router.get("/sessions/{session_id}/transcript/verify")
async def transcript_verify(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Recompute the transcript hash against the audit-chain seal — the same
    offline-style attestation the recording has."""
    import hashlib as _hashlib

    row = await _sealed_transcript(session, user, session_id)
    try:
        plaintext = await _unseal_transcript(session, row)
    except Exception:
        return {"verified": False, "reason": "unseal_failed (tampered or key mismatch)"}
    actual = _hashlib.sha256(plaintext).hexdigest()
    ref = row.transcript_anchor_ref or ""
    sealed_hash = next((p.split("sha256:", 1)[1] for p in ref.split(";") if p.startswith("sha256:")), None)
    return {
        "verified":      sealed_hash == actual,
        "sha256":        actual,
        "sealed_sha256": sealed_hash,
        "anchor_ref":    ref,
    }


class GuestPreviewBody(BaseModel):
    invite_token: str


@router.post("/guest/preview")
async def guest_preview(
    body: GuestPreviewBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Public, non-consuming invite preview — the guest page must show the
    recording notice BEFORE the consent act (the exchange). Uniform 404."""
    allowed, _retry = _guest_rate.is_allowed(request.client.host if request.client else "unknown")
    if not allowed:
        raise HTTPException(429, "Too many attempts")
    try:
        return await meet.preview_guest_invite(session, body.invite_token)
    except ValueError:
        raise HTTPException(404, "Invite not found")


@router.post("/webhook/livekit")
async def livekit_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
):
    """LiveKit server webhooks (presence + room lifecycle). Authenticated by
    the signed body-hash JWT LiveKit sends — anything unverifiable is 401."""
    raw = await request.body()
    if not livekit.verify_webhook(authorization, raw):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "Invalid webhook body")
    await meet.handle_livekit_event(session, event)
    return {"ok": True}


# ═══ HxMeet P4a — session intelligence (local transcription + summary) ═══
# Opt-in per tenant (tenant.settings["meet"].intelligence, default OFF) —
# analyzing recorded conversations is a policy decision even when every model
# runs locally. faster-whisper missing = 501 fail-closed. The analysis runs
# as a background job; every run is audit-chained.

from fastapi import BackgroundTasks  # noqa: E402
from case_service.meet import intelligence as meet_intel  # noqa: E402


@router.post("/sessions/{session_id}/intelligence", status_code=202)
async def intelligence_run(
    session_id: str,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import CaseSessionIntelligenceModel

    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.intelligence.run")
    has_recording = row.recording_status == "sealed" and row.recording_document_id is not None
    has_transcript = row.transcript_status == "sealed" and row.transcript_document_id is not None
    if not has_recording and not has_transcript:
        raise HTTPException(404, "No sealed recording for this session")
    if not await meet_intel.tenant_intelligence_enabled(session, row.tenant_id):
        raise HTTPException(400, "Session intelligence is not enabled for this tenant")
    # Whisper is only needed when there's no sealed live transcript to reuse.
    if not has_transcript and not meet_intel.whisper_available():
        raise HTTPException(501, "Transcription is not installed on this server (faster-whisper)")

    intel = await session.get(CaseSessionIntelligenceModel, row.id)
    if intel is not None and intel.status == "running":
        raise HTTPException(409, "Analysis is already running for this session")
    if intel is None:
        intel = CaseSessionIntelligenceModel(session_id=row.id, requested_by=user.user_id)
        session.add(intel)
    else:
        intel.status = "pending"
        intel.error = None
        intel.requested_by = user.user_id
    await session.commit()

    background.add_task(meet_intel.run_intelligence_job, row.id)
    return {"session_id": str(row.id), "status": "pending"}


# ═══ HxMeet P4a-live — streaming captions (browser-stream MVP) ═══
# Each participant's browser sends its OWN mic audio (int16 mono 16 kHz PCM)
# over this WebSocket; the server's auto-detected Whisper backend transcribes
# and returns partial/final captions, which the sender fans out to the room
# on the LiveKit data channel. Audio never leaves this server.
#
# Auth = the LiveKit access token we minted (HS256, room-pinned): uniform
# proof of room membership for workers AND guests, no platform JWT needed.
# Tenant opt-in tenant.settings["meet"].live_captions, default OFF.

from fastapi import WebSocket, WebSocketDisconnect  # noqa: E402
from case_service.meet import asr as meet_asr  # noqa: E402


async def tenant_live_captions_enabled(session: AsyncSession, tenant_slug: str | None) -> bool:
    from case_service.db.models import TenantModel
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (tenant_slug or "default"))
    )).scalars().first()
    return bool(((tenant.settings or {}).get("meet", {}) if tenant else {}).get("live_captions", False))


@router.websocket("/sessions/{session_id}/captions")
async def captions_stream(
    websocket: WebSocket,
    session_id: str,
    session: AsyncSession = Depends(get_session),
):
    await websocket.accept()

    # First frame must be the auth handshake: {"token": <livekit access token>}.
    # Every failure mode closes with the same code — no oracle.
    try:
        hello = json.loads(await websocket.receive_text())
    except Exception:
        await websocket.close(code=4404)
        return

    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        await websocket.close(code=4404)
        return
    row = (await session.execute(
        select(CaseSessionModel).where(CaseSessionModel.id == sid)
    )).scalar_one_or_none()
    if row is None or row.driver != "embedded" or row.status != "active":
        await websocket.close(code=4404)
        return
    room = row.external_meeting_id or livekit.room_name(row.tenant_id or "default", row.id)
    claims = livekit.verify_access_token(str(hello.get("token", "")), room=room)
    if claims is None:
        await websocket.close(code=4404)
        return
    if not await tenant_live_captions_enabled(session, row.tenant_id):
        await websocket.close(code=4403, reason="Live captions are not enabled for this tenant")
        return
    if not meet_asr.available():
        await websocket.close(code=4501, reason="ASR engine is not installed on this server")
        return

    identity = claims.get("sub") or "unknown"
    from case_service.api.routers.cases import _audit
    await _audit(session, row.case_id, "captions_enabled", actor_id=identity,
                 details={"session_id": str(row.id)})
    await session.commit()

    stream = meet_asr.CaptionStream()
    try:
        info = meet_asr.info()
        await websocket.send_json({"type": "ready",
                                   "lag_mode": bool(info and not info.realtime)})
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            chunk = message.get("bytes")
            if not chunk:
                continue
            caption = await stream.feed(chunk)
            if caption:
                await websocket.send_json({"type": "caption", **caption})
                # P4a-live-2: finalized segments of a record-intent session are
                # staged for sealing. Speaker = the VERIFIED token identity —
                # never anything the client sent.
                if caption.get("is_final"):
                    await meet.stage_caption_segment(
                        session, row, speaker=identity, text=caption["text"])
    except WebSocketDisconnect:
        pass


@router.get("/sessions/{session_id}/intelligence")
async def intelligence_get(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import CaseSessionIntelligenceModel

    row = await _visible_session(session, user, session_id)
    # Reading the analysis derives from the recording — same gate as viewing it.
    await _authorized_case(session, user, str(row.case_id), "meet.recording.view")
    intel = await session.get(CaseSessionIntelligenceModel, row.id)
    return meet_intel.intelligence_view(intel)


# ═══ HxMeet P4c — Video-KYC liveness challenges ═══
# Challenges are minted server-side (CSPRNG — the far end can never predict
# them) and only while a record-intent session is actually RECORDING, so the
# instruction and the response both land in the sealed recording. The worker
# records the observed result per challenge; nothing here auto-passes.
# Tenant opt-in tenant.settings["meet"].kyc, default OFF.

from case_service.meet import kyc as meet_kyc  # noqa: E402


class IssueChallengesBody(BaseModel):
    kinds: list[str] | None = None   # default: one of each kind


@router.post("/sessions/{session_id}/challenges", status_code=201)
async def challenges_issue(
    session_id: str,
    body: IssueChallengesBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.kyc.run")
    _require_active_embedded(row)
    if not await meet_kyc.tenant_kyc_enabled(session, row.tenant_id):
        raise HTTPException(400, "Video-KYC is not enabled for this tenant")
    if not row.record_intent:
        raise HTTPException(409, "Challenges require a record-intent session")
    if row.recording_status != "recording":
        raise HTTPException(409, "Challenges can only be issued while the recording is running")
    kinds = body.kinds or list(meet_kyc.CHALLENGE_KINDS)
    unknown = [k for k in kinds if k not in meet_kyc.CHALLENGE_KINDS]
    if unknown:
        raise HTTPException(400, f"Unknown challenge kinds: {', '.join(unknown)}")
    challenges = await meet_kyc.issue_challenges(
        session, row=row, kinds=kinds, actor=user.user_id)
    return {"challenges": [meet_kyc.challenge_view(c) for c in challenges]}


@router.get("/sessions/{session_id}/challenges")
async def challenges_list(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "case.read")
    rows = await meet_kyc.list_challenges(session, row.id)
    return {"challenges": [meet_kyc.challenge_view(c) for c in rows]}


# ── P4c-2: passive signal pass on the sealed recording ──
# One risk score with a per-check breakdown, never a binary verdict. Checks
# whose dependency is missing are skipped honestly; 501 only when NO check
# could run (no frame-analysis deps AND no sealed transcript to cross-check).

@router.post("/sessions/{session_id}/kyc-analysis", status_code=202)
async def kyc_analysis_run(
    session_id: str,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import CaseSessionKycSignalsModel

    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.kyc.run")
    has_recording = row.recording_status == "sealed" and row.recording_document_id is not None
    has_transcript = row.transcript_status == "sealed" and row.transcript_document_id is not None
    if not has_recording and not has_transcript:
        raise HTTPException(404, "No sealed recording for this session")
    if not await meet_kyc.tenant_kyc_enabled(session, row.tenant_id):
        raise HTTPException(400, "Video-KYC is not enabled for this tenant")
    if not (has_recording and meet_kyc.cv_available()) and not has_transcript:
        raise HTTPException(501, "KYC analysis is not installed on this server (pip install .[kyc])")

    signals = await session.get(CaseSessionKycSignalsModel, row.id)
    if signals is not None and signals.status == "running":
        raise HTTPException(409, "KYC analysis is already running for this session")
    if signals is None:
        signals = CaseSessionKycSignalsModel(session_id=row.id, requested_by=user.user_id)
        session.add(signals)
    else:
        signals.status = "pending"
        signals.error = None
        signals.requested_by = user.user_id
    await session.commit()

    background.add_task(meet_kyc.run_kyc_job, row.id)
    return {"session_id": str(row.id), "status": "pending"}


@router.get("/sessions/{session_id}/kyc-analysis")
async def kyc_analysis_get(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import CaseSessionKycSignalsModel

    row = await _visible_session(session, user, session_id)
    # Reading the analysis derives from the recording — same gate as viewing it.
    await _authorized_case(session, user, str(row.case_id), "meet.recording.view")
    signals = await session.get(CaseSessionKycSignalsModel, row.id)
    threshold = await meet_kyc.tenant_review_threshold(session, row.tenant_id)
    return meet_kyc.kyc_view(signals, threshold)


class ChallengeResultBody(BaseModel):
    result: str                # passed | failed | skipped
    notes: str | None = None


@router.post("/sessions/{session_id}/challenges/{challenge_id}/result")
async def challenge_result(
    session_id: str,
    challenge_id: str,
    body: ChallengeResultBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    row = await _visible_session(session, user, session_id)
    await _authorized_case(session, user, str(row.case_id), "meet.kyc.run")
    if body.result not in meet_kyc.CHALLENGE_RESULTS:
        raise HTTPException(400, "result must be one of: " + ", ".join(meet_kyc.CHALLENGE_RESULTS))
    challenge = await meet_kyc.record_challenge_result(
        session, row=row, challenge_id=challenge_id,
        result=body.result, notes=body.notes, actor=user.user_id)
    if challenge is None:
        raise HTTPException(404, "Challenge not found")
    return meet_kyc.challenge_view(challenge)
