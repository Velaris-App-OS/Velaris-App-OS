"""HxMeet — real-time case sessions (P1 off-platform + P2 embedded drivers).

One provider-agnostic CaseSession, two first-party drivers (fail-closed set,
never a plugin): `off_platform` (P1) creates the meeting on the tenant's own
provider (Teams / Zoom / Google Meet / generic) through an HxBridge connector
— media and recording stay with the provider, stated plainly in the UI.
`embedded` (P2) runs on self-hosted LiveKit: rooms are tenant-namespaced,
join tokens are minted server-side (room-scoped, identity-pinned, short-TTL),
guests join via single-use invites bound to a portal-customer or email
principal — never an ambient "anyone with the link" room. No recording until
P3.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.config import get_settings
from case_service.db.models import (
    CaseSessionCaptionSegmentModel,
    CaseSessionModel,
    CaseSessionParticipantModel,
    ConnectorRegistryModel,
    PortalCustomerModel,
    TenantModel,
)
from case_service.hxbridge.encryption import decrypt_credentials
from case_service.hxbridge.protocol import get_connector
from case_service.meet import livekit

logger = logging.getLogger(__name__)

# First-party, fail-closed: the only connector types the off_platform driver
# will drive. Same spine as the DB-backend allowlist — adding one is a
# platform release, never configuration.
MEETING_CONNECTOR_TYPES = ("teams", "zoom", "gmeet", "meet_generic")

_PROVIDER_BY_TYPE = {"teams": "teams", "zoom": "zoom", "gmeet": "gmeet", "meet_generic": "generic"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _emit(case_id: uuid.UUID, event_type: str, data: dict) -> None:
    try:
        from case_service.hxstream.emitter import emit_trace
        await emit_trace(event_type, data, case_id=case_id)
    except Exception as exc:
        logger.warning("HxStream emit failed (%s): %s", event_type, exc)


async def _log_event(session: AsyncSession, case_id: uuid.UUID, case_type_id, activity: str, actor: str) -> None:
    try:
        from case_service.process_mining.event_logger import log_event
        await log_event(session, case_id=case_id, case_type_id=case_type_id,
                        activity=activity, activity_type="session", actor_id=actor)
    except Exception as exc:
        logger.warning("Session event log failed (non-fatal): %s", exc)


async def tenant_meet_settings(session: AsyncSession, tenant_id: str) -> dict:
    """Per-tenant HxMeet config from tenant.settings["meet"] ({} when unset)."""
    row = (await session.execute(
        select(TenantModel).where(TenantModel.slug == tenant_id)
    )).scalar_one_or_none()
    return ((row.settings or {}).get("meet", {}) if row else {}) or {}


async def resolve_connector(
    session: AsyncSession,
    tenant_id: str,
    provider: str | None = None,
    connector_id: uuid.UUID | None = None,
) -> ConnectorRegistryModel:
    """Pick the meeting connector for a session (fail-closed to MEETING_CONNECTOR_TYPES).

    Precedence: explicit connector_id → explicit provider → tenant default
    (tenant.settings["meet"]) → the only enabled meeting connector.
    """
    q = select(ConnectorRegistryModel).where(
        ConnectorRegistryModel.connector_type.in_(MEETING_CONNECTOR_TYPES),
        ConnectorRegistryModel.enabled == True,  # noqa: E712
        # NULL tenant = platform-shared connector (hxbridge registers them
        # without a tenant); a tenant-scoped row still only matches its tenant.
        or_(ConnectorRegistryModel.tenant_id == tenant_id,
            ConnectorRegistryModel.tenant_id.is_(None)),
    )
    if connector_id:
        q = q.where(ConnectorRegistryModel.id == connector_id)
    elif provider:
        by_provider = {v: k for k, v in _PROVIDER_BY_TYPE.items()}
        ctype = by_provider.get(provider, provider)
        if ctype not in MEETING_CONNECTOR_TYPES:
            raise ValueError(f"Unknown meeting provider '{provider}'")
        q = q.where(ConnectorRegistryModel.connector_type == ctype)
    else:
        cfg = await tenant_meet_settings(session, tenant_id)
        if cfg.get("connector_id"):
            q = q.where(ConnectorRegistryModel.id == uuid.UUID(cfg["connector_id"]))
        elif cfg.get("provider"):
            return await resolve_connector(session, tenant_id, provider=cfg["provider"])

    rows = (await session.execute(q.limit(2))).scalars().all()
    if not rows:
        raise ValueError("No enabled meeting connector configured for this tenant")
    return rows[0]


async def start_session(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_id,
    tenant_id: str,
    started_by: str,
    title: str | None = None,
    provider: str | None = None,
    connector_id: uuid.UUID | None = None,
) -> CaseSessionModel:
    """Create a meeting on the provider and attach it to the case."""
    connector_row = await resolve_connector(session, tenant_id, provider, connector_id)
    impl = get_connector(
        connector_row.connector_type,
        config=connector_row.config or {},
        credentials=decrypt_credentials(connector_row.credentials),
    )
    result = await impl.execute({"title": title or "Velaris case session"})

    row = CaseSessionModel(
        case_id=case_id,
        tenant_id=tenant_id,
        driver="off_platform",
        provider=_PROVIDER_BY_TYPE[connector_row.connector_type],
        connector_id=connector_row.id,
        status="active",
        title=title,
        external_meeting_id=result.get("external_meeting_id"),
        join_url=result["join_url"],
        started_by=started_by,
        started_at=_utcnow(),
    )
    session.add(row)
    await session.flush()

    await _log_event(session, case_id, case_type_id, "session_started", started_by)
    await session.commit()
    await session.refresh(row)
    await _emit(case_id, "meet.session_started", {
        "session_id": str(row.id), "provider": row.provider, "started_by": started_by,
    })
    return row


async def end_session(
    session: AsyncSession,
    *,
    row: CaseSessionModel,
    case_type_id,
    actor: str,
    cancelled: bool = False,
) -> CaseSessionModel:
    row.status = "cancelled" if cancelled else "ended"
    row.ended_at = _utcnow()
    session.add(row)
    await _log_event(session, row.case_id, case_type_id,
                     "session_cancelled" if cancelled else "session_ended", actor)
    await session.commit()
    await session.refresh(row)
    # P4a-live-2: seal any staged live-caption transcript. Never blocks the
    # end — a seal failure is recorded on the row, not raised to the caller.
    try:
        await seal_live_transcript(session, row)
    except Exception as exc:
        logger.warning("Live transcript seal failed for session %s: %s", row.id, exc)
        row.transcript_status = "failed"
        session.add(row)
        await session.commit()
        await session.refresh(row)
    await _emit(row.case_id, "meet.session_ended", {
        "session_id": str(row.id), "status": row.status, "actor": actor,
    })
    return row


async def list_sessions(session: AsyncSession, case_id: uuid.UUID) -> list[CaseSessionModel]:
    return list((await session.execute(
        select(CaseSessionModel)
        .where(CaseSessionModel.case_id == case_id)
        .order_by(CaseSessionModel.created_at.desc())
    )).scalars().all())


# ── P2: embedded driver (self-hosted LiveKit) ────────────────────────────────

async def resolve_driver(session: AsyncSession, tenant_id: str) -> str:
    """Per-tenant driver: tenant.settings["meet"].driver > platform default."""
    cfg = await tenant_meet_settings(session, tenant_id)
    return cfg.get("driver") or get_settings().meet_driver


async def start_embedded_session(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_id,
    tenant_id: str,
    started_by: str,
    title: str | None = None,
    record_intent: bool = False,
) -> CaseSessionModel:
    """Create an embedded (LiveKit) session — no provider call, no join_url;
    Studio opens the in-tab room and every join mints its own token.
    record_intent (P3) is declared here and immutable for the session's life:
    every subsequent join sees the recording notice and stamps consent."""
    row = CaseSessionModel(
        case_id=case_id,
        tenant_id=tenant_id,
        driver="embedded",
        provider="livekit",
        status="active",
        title=title,
        record_intent=record_intent,
        started_by=started_by,
        started_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    row.external_meeting_id = livekit.room_name(tenant_id, row.id)

    await _log_event(session, case_id, case_type_id, "session_started", started_by)
    await session.commit()
    await session.refresh(row)
    await _emit(case_id, "meet.session_started", {
        "session_id": str(row.id), "provider": row.provider, "started_by": started_by,
    })
    return row


async def _participant_by_identity(
    session: AsyncSession, session_id: uuid.UUID, identity: str,
) -> CaseSessionParticipantModel | None:
    return (await session.execute(
        select(CaseSessionParticipantModel).where(
            CaseSessionParticipantModel.session_id == session_id,
            CaseSessionParticipantModel.identity == identity,
        ).limit(1)
    )).scalars().first()


async def mint_worker_token(
    session: AsyncSession,
    *,
    row: CaseSessionModel,
    user_id: str,
    display_name: str | None = None,
) -> dict:
    """Room token for an authenticated worker (HxGuard-checked by the caller)."""
    identity = f"user:{user_id}"
    participant = await _participant_by_identity(session, row.id, identity)
    if participant is None:
        participant = CaseSessionParticipantModel(
            session_id=row.id, tenant_id=row.tenant_id,
            identity=identity, display_name=display_name, role="host",
        )
        session.add(participant)
    # P3: on a record-intent session, taking a join token IS the consent act —
    # Studio shows the recording notice before requesting one.
    if row.record_intent and participant.consent_recorded_at is None:
        participant.consent_recorded_at = _utcnow()
        session.add(participant)
    await session.commit()
    room = row.external_meeting_id or livekit.room_name(row.tenant_id or "default", row.id)
    return {
        "url":      get_settings().livekit_url,
        "token":    livekit.mint_access_token(room=room, identity=identity, display_name=display_name),
        "room":     room,
        "identity": identity,
    }


async def join_customer(
    session: AsyncSession,
    *,
    row: CaseSessionModel,
    customer_id: uuid.UUID,
    display_name: str | None = None,
) -> dict:
    """Room token for a logged-in portal customer who was invited to this
    session. Requires an existing participant row for `customer:{id}` — the
    worker's invite created it — so internal-only sessions on the customer's
    case stay invisible and unjoinable. Unlike the emailed invite link, this
    path is re-usable while the session is active (the single-use link only
    guards the anonymous route).
    """
    identity = f"customer:{customer_id}"
    participant = await _participant_by_identity(session, row.id, identity)
    if participant is None:
        raise ValueError("not invited")
    if display_name:
        participant.display_name = participant.display_name or display_name
    # Same consent semantics as the guest exchange: the portal shows the
    # recording notice before requesting a token — the request is the consent.
    if row.record_intent and participant.consent_recorded_at is None:
        participant.consent_recorded_at = _utcnow()
    session.add(participant)
    await session.commit()
    room = row.external_meeting_id or livekit.room_name(row.tenant_id or "default", row.id)
    return {
        "url":           get_settings().livekit_url,
        "token":         livekit.mint_access_token(room=room, identity=identity,
                                                   display_name=participant.display_name),
        "room":          room,
        "identity":      identity,
        "display_name":  participant.display_name,
        "title":         row.title,
        "session_id":    str(row.id),
        "record_intent": row.record_intent,
    }


async def create_guest_invite(
    session: AsyncSession,
    *,
    row: CaseSessionModel,
    invited_by: str,
    customer_id: uuid.UUID | None = None,
    email: str | None = None,
    display_name: str | None = None,
) -> tuple[CaseSessionParticipantModel, str]:
    """Single-use, short-TTL invite pinned to a guest principal.

    The principal is a portal customer (preferred) or a bare email — either
    way a per-session identity the inviting worker is accountable for. Only
    the SHA-256 of the token is stored; the raw token is returned once.
    """
    if customer_id is not None:
        customer = (await session.execute(
            select(PortalCustomerModel)
            .join(TenantModel, PortalCustomerModel.tenant_id == TenantModel.id)
            .where(PortalCustomerModel.id == customer_id,
                   TenantModel.slug == (row.tenant_id or "default"))
        )).scalars().first()
        if customer is None:
            raise ValueError("Unknown customer for this tenant")
        identity = f"customer:{customer.id}"
        display_name = display_name or customer.display_name
    elif email:
        identity = f"email:{email.strip().lower()}"
        display_name = display_name or email
    else:
        raise ValueError("Invite needs a customer_id or an email")

    raw_token = secrets.token_urlsafe(32)
    participant = CaseSessionParticipantModel(
        session_id=row.id,
        tenant_id=row.tenant_id,
        identity=identity,
        display_name=display_name,
        role="guest",
        invited_by=invited_by,
        invite_token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        invite_expires_at=_utcnow() + timedelta(seconds=get_settings().meet_guest_invite_ttl_seconds),
    )
    session.add(participant)
    await session.commit()
    await session.refresh(participant)
    return participant, raw_token


async def exchange_guest_invite(session: AsyncSession, raw_token: str) -> dict:
    """Trade a valid, unexpired, UNUSED invite for a room token (atomic consume).

    Every failure mode raises the same ValueError — the public endpoint maps
    it to a uniform 404 (no oracle for token guessing).
    """
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    consumed = await session.execute(
        update(CaseSessionParticipantModel)
        .where(CaseSessionParticipantModel.invite_token_hash == token_hash,
               CaseSessionParticipantModel.token_used_at.is_(None))
        .values(token_used_at=_utcnow())
    )
    await session.commit()
    if consumed.rowcount != 1:
        raise ValueError("invalid invite")

    participant = (await session.execute(
        select(CaseSessionParticipantModel)
        .where(CaseSessionParticipantModel.invite_token_hash == token_hash)
    )).scalars().first()
    now = _utcnow()
    expires = participant.invite_expires_at
    if expires is not None and expires.tzinfo is None:      # SQLite returns naive
        expires = expires.replace(tzinfo=timezone.utc)
    if expires is None or expires < now:
        raise ValueError("invalid invite")

    row = (await session.execute(
        select(CaseSessionModel).where(CaseSessionModel.id == participant.session_id)
    )).scalars().first()
    if row is None or row.status != "active" or row.driver != "embedded":
        raise ValueError("invalid invite")

    # P3: the guest join page shows the recording notice before the exchange —
    # exchanging on a record-intent session is the guest's consent act.
    if row.record_intent and participant.consent_recorded_at is None:
        participant.consent_recorded_at = _utcnow()
        session.add(participant)
        await session.commit()

    room = row.external_meeting_id or livekit.room_name(row.tenant_id or "default", row.id)
    return {
        "url":           get_settings().livekit_url,
        "token":         livekit.mint_access_token(room=room, identity=participant.identity,
                                                   display_name=participant.display_name),
        "room":          room,
        "identity":      participant.identity,
        "display_name":  participant.display_name,
        "title":         row.title,
        "session_id":    str(row.id),
        "record_intent": row.record_intent,
    }


async def preview_guest_invite(session: AsyncSession, raw_token: str) -> dict:
    """Non-consuming look at a valid invite — the guest page needs the
    recording notice BEFORE the consent act (the exchange). Same uniform
    failure as the exchange: any invalid/used/expired/dead token raises."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    participant = (await session.execute(
        select(CaseSessionParticipantModel)
        .where(CaseSessionParticipantModel.invite_token_hash == token_hash,
               CaseSessionParticipantModel.token_used_at.is_(None))
    )).scalars().first()
    if participant is None:
        raise ValueError("invalid invite")
    expires = participant.invite_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires is None or expires < _utcnow():
        raise ValueError("invalid invite")
    row = (await session.execute(
        select(CaseSessionModel).where(CaseSessionModel.id == participant.session_id)
    )).scalars().first()
    if row is None or row.status != "active" or row.driver != "embedded":
        raise ValueError("invalid invite")
    return {
        "title":         row.title,
        "display_name":  participant.display_name,
        "record_intent": row.record_intent,
    }


async def list_participants(
    session: AsyncSession, session_id: uuid.UUID,
) -> list[CaseSessionParticipantModel]:
    return list((await session.execute(
        select(CaseSessionParticipantModel)
        .where(CaseSessionParticipantModel.session_id == session_id)
        .order_by(CaseSessionParticipantModel.created_at)
    )).scalars().all())


# ── P3: sealed recording ─────────────────────────────────────────────────────

RECORDING_AAD_PREFIX = "hxmeet-recording:"
TRANSCRIPT_AAD_PREFIX = "hxmeet-transcript:"


# ── P4a-live-2: seal the live transcript (the recording's twin) ─────────────

async def stage_caption_segment(
    session: AsyncSession, row: CaseSessionModel, *, speaker: str, text: str,
) -> None:
    """Stage one finalized caption segment for later sealing. Only for
    record-intent sessions (join consent covers the transcript) that are
    still active — anything else is ephemeral by design."""
    if not row.record_intent:
        return
    status = (await session.execute(
        select(CaseSessionModel.status).where(CaseSessionModel.id == row.id)
    )).scalar_one_or_none()
    if status != "active":
        return
    session.add(CaseSessionCaptionSegmentModel(
        session_id=row.id, tenant_id=row.tenant_id, speaker=speaker, text=text,
    ))
    await session.commit()


async def seal_live_transcript(session: AsyncSession, row: CaseSessionModel) -> None:
    """Compose staged caption segments and seal them to the case exactly like
    the recording: sha256 (plaintext) > tenant-DEK seal (AAD pins the session)
    > .hxsealed case document > audit-chain entry + anchor ref (+ best-effort
    TSA). Staging rows are deleted after the seal — the plaintext conversation
    never rests in the DB. Idempotent: already sealed or nothing staged = no-op."""
    from sqlalchemy import delete as _delete

    from case_service.compliance.audit_chain import seal_new_entries
    from case_service.documents.service import DocumentService
    from case_service.hxvault import crypto as vault
    from case_service.hxvault.keyring import ensure_dek

    if row.transcript_status == "sealed":
        return
    segments = list((await session.execute(
        select(CaseSessionCaptionSegmentModel)
        .where(CaseSessionCaptionSegmentModel.session_id == row.id)
        .order_by(CaseSessionCaptionSegmentModel.spoken_at,
                  CaseSessionCaptionSegmentModel.created_at)
    )).scalars().all())
    if not segments:
        return

    # Verified token identities > display names where the participant is known.
    names = {p.identity: (p.display_name or p.identity) for p in (await session.execute(
        select(CaseSessionParticipantModel)
        .where(CaseSessionParticipantModel.session_id == row.id)
    )).scalars().all()}

    lines = [f"Live transcript — {row.title or 'Case session'} (session {row.id})",
             f"Generated from live captions; assistive, may mis-hear. Speakers are verified room-token identities.",
             ""]
    for seg in segments:
        stamp = seg.spoken_at.strftime("%H:%M:%S") if seg.spoken_at else "--:--:--"
        lines.append(f"[{stamp}] {names.get(seg.speaker, seg.speaker)}: {seg.text}")
    plaintext = "\n".join(lines).encode()
    sha256 = hashlib.sha256(plaintext).hexdigest()

    tenant_row = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (row.tenant_id or "default"))
    )).scalars().first()
    dek = await ensure_dek(session, tenant_row.id if tenant_row else None)
    sealed = vault.seal(dek, plaintext, f"{TRANSCRIPT_AAD_PREFIX}{row.id}".encode())

    doc = await DocumentService().upload(
        session, case_id=row.case_id,
        filename=f"session-{row.id}-transcript.txt.hxsealed",
        data=sealed, content_type="application/octet-stream",
        uploaded_by="hxmeet", tenant_id=row.tenant_id,
    )

    await _audit(session, row.case_id, "meet.transcript.sealed", "hxmeet", {
        "session_id": str(row.id), "document_id": str(doc.id),
        "sha256": sha256, "size_bytes": len(plaintext), "segments": len(segments),
    })
    chain = await seal_new_entries(session)
    row.transcript_document_id = doc.id
    row.transcript_status = "sealed"
    row.transcript_anchor_ref = f"chain-seq:{chain['tip_sequence']};tip:{chain['tip_hash']};sha256:{sha256}"
    session.add(row)
    await session.execute(_delete(CaseSessionCaptionSegmentModel)
                          .where(CaseSessionCaptionSegmentModel.session_id == row.id))
    await session.commit()
    await session.refresh(row)

    try:  # TSA anchoring is external + optional — never block the seal on it
        from case_service.compliance.audit_anchor import anchor_chain_tip
        await anchor_chain_tip(session)
    except Exception as exc:
        logger.warning("TSA anchor after transcript seal failed (non-fatal): %s", exc)
    await _emit(row.case_id, "meet.transcript_sealed", {
        "session_id": str(row.id), "document_id": str(doc.id), "sha256": sha256,
    })


def recording_available() -> bool:
    """Recording fail-closes unless LiveKit AND the shared egress dir are set."""
    return livekit.configured() and bool(get_settings().meet_recordings_dir)


async def _joined_without_consent(session: AsyncSession, session_id: uuid.UUID) -> list[str]:
    """Identities currently in the room (joined, not left) lacking a consent stamp."""
    rows = (await session.execute(
        select(CaseSessionParticipantModel).where(
            CaseSessionParticipantModel.session_id == session_id,
            CaseSessionParticipantModel.joined_at.is_not(None),
            CaseSessionParticipantModel.left_at.is_(None),
            CaseSessionParticipantModel.consent_recorded_at.is_(None),
        )
    )).scalars().all()
    return [p.identity for p in rows]


async def _audit(session: AsyncSession, case_id: uuid.UUID, action: str,
                 actor: str, details: dict) -> None:
    from case_service.db import repository as repo
    await repo.append_audit_entry(session, data={
        "case_id": case_id, "action": action, "actor_id": actor,
        "actor_type": "system" if actor == "hxmeet" else "user",
        "details": details,
    })


async def start_recording(session: AsyncSession, *, row: CaseSessionModel, actor: str) -> CaseSessionModel:
    """Start the egress. Consent is a hard gate: every participant currently
    in the room must carry a consent stamp — legal exposure, not a nicety."""
    if not row.record_intent:
        raise ValueError("Session was not started with recording intent")
    if row.recording_status not in ("none", "failed"):
        raise ValueError(f"Recording is already {row.recording_status}")
    missing = await _joined_without_consent(session, row.id)
    if missing:
        raise ValueError(f"Participants without recording consent: {', '.join(sorted(missing))}")

    room = row.external_meeting_id or livekit.room_name(row.tenant_id or "default", row.id)
    fname = f"{room}-{uuid.uuid4().hex[:8]}.mp4"
    # /out is the egress container's mount of meet_recordings_dir (host side).
    egress_id = await livekit.start_room_recording(room, f"/out/{fname}")

    row.recording_status = "recording"
    row.recording_egress_id = egress_id
    session.add(row)
    await _audit(session, row.case_id, "meet.recording.started", actor,
                 {"session_id": str(row.id), "egress_id": egress_id})
    await session.commit()
    await session.refresh(row)
    await _emit(row.case_id, "meet.recording_started", {"session_id": str(row.id)})
    return row


async def stop_recording(session: AsyncSession, *, row: CaseSessionModel, actor: str) -> CaseSessionModel:
    if row.recording_status != "recording":
        raise ValueError(f"No active recording (status: {row.recording_status})")
    await livekit.stop_room_recording(row.recording_egress_id)
    row.recording_status = "processing"   # sealed on the egress_ended webhook
    session.add(row)
    await _audit(session, row.case_id, "meet.recording.stopped", actor,
                 {"session_id": str(row.id), "egress_id": row.recording_egress_id})
    await session.commit()
    await session.refresh(row)
    await _emit(row.case_id, "meet.recording_stopped", {"session_id": str(row.id)})
    return row


async def ingest_recording(session: AsyncSession, row: CaseSessionModel, egress_filename: str) -> None:
    """Seal a finished egress file to the case: sha256 (plaintext) > tenant-DEK
    seal > case document > audit-chain entry (+ best-effort TSA anchor). The
    plaintext hash goes into the tamper-evident chain BEFORE anything else can
    touch the file; the temp file is deleted after ingest."""
    import os

    from case_service.compliance.audit_chain import seal_new_entries
    from case_service.documents.service import DocumentService
    from case_service.hxvault import crypto as vault
    from case_service.hxvault.keyring import ensure_dek

    fname = os.path.basename(egress_filename)
    path = os.path.join(get_settings().meet_recordings_dir, fname)
    with open(path, "rb") as fh:
        plaintext = fh.read()
    sha256 = hashlib.sha256(plaintext).hexdigest()

    # Per-tenant DEK: cross-tenant recording access impossible by key
    # separation, not just row filters. Slug > tenant UUID > DEK.
    tenant_row = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (row.tenant_id or "default"))
    )).scalars().first()
    dek = await ensure_dek(session, tenant_row.id if tenant_row else None)
    sealed = vault.seal(dek, plaintext, f"{RECORDING_AAD_PREFIX}{row.id}".encode())

    doc = await DocumentService().upload(
        session, case_id=row.case_id,
        filename=f"session-{row.id}-recording.mp4.hxsealed",
        data=sealed, content_type="application/octet-stream",
        uploaded_by="hxmeet", tenant_id=row.tenant_id,
    )

    await _audit(session, row.case_id, "meet.recording.sealed", "hxmeet", {
        "session_id": str(row.id), "document_id": str(doc.id),
        "sha256": sha256, "size_bytes": len(plaintext),
        "egress_id": row.recording_egress_id,
    })
    chain = await seal_new_entries(session)
    row.recording_document_id = doc.id
    row.recording_status = "sealed"
    row.audit_anchor_ref = f"chain-seq:{chain['tip_sequence']};tip:{chain['tip_hash']};sha256:{sha256}"
    session.add(row)
    await session.commit()
    await session.refresh(row)

    try:
        os.unlink(path)
    except OSError as exc:
        logger.warning("Recording temp file cleanup failed (%s): %s", path, exc)
    try:  # TSA anchoring is external + optional — never block the seal on it
        from case_service.compliance.audit_anchor import anchor_chain_tip
        await anchor_chain_tip(session)
    except Exception as exc:
        logger.warning("TSA anchor after recording seal failed (non-fatal): %s", exc)
    await _emit(row.case_id, "meet.recording_sealed", {
        "session_id": str(row.id), "document_id": str(doc.id), "sha256": sha256,
    })


async def handle_egress_event(session: AsyncSession, event: dict) -> None:
    """egress_ended: seal on success, mark failed otherwise. Idempotent —
    a session already sealed/failed is left alone."""
    info = event.get("egressInfo") or event.get("egress_info") or {}
    egress_id = info.get("egressId") or info.get("egress_id")
    if not egress_id:
        return
    row = (await session.execute(
        select(CaseSessionModel).where(CaseSessionModel.recording_egress_id == egress_id)
    )).scalars().first()
    if row is None or row.recording_status not in ("recording", "processing"):
        return

    status = (info.get("status") or "").upper()
    files = info.get("fileResults") or info.get("file_results") or []
    filename = files[0].get("filename") if files else None
    if status == "EGRESS_COMPLETE" and filename:
        try:
            await ingest_recording(session, row, filename)
            return
        except Exception as exc:
            logger.error("Recording ingest failed for session %s: %s", row.id, exc)
    row.recording_status = "failed"
    session.add(row)
    await _audit(session, row.case_id, "meet.recording.failed", "hxmeet",
                 {"session_id": str(row.id), "egress_id": egress_id, "egress_status": status})
    await session.commit()
    await _emit(row.case_id, "meet.recording_failed", {"session_id": str(row.id)})


async def handle_livekit_event(session: AsyncSession, event: dict) -> None:
    """Apply a verified LiveKit webhook: presence stamps + auto-end.

    Unknown rooms/sessions are ignored (the webhook is platform-wide);
    room_finished is idempotent with the manual end path.
    """
    kind = event.get("event")
    if kind == "egress_ended":
        await handle_egress_event(session, event)
        return
    sid = livekit.parse_room_session_id((event.get("room") or {}).get("name") or "")
    if sid is None or kind not in ("participant_joined", "participant_left", "room_finished"):
        return
    row = (await session.execute(
        select(CaseSessionModel).where(CaseSessionModel.id == sid)
    )).scalars().first()
    if row is None or row.driver != "embedded":
        return

    if kind == "room_finished":
        if row.recording_status == "recording":
            # Egress ends itself when the room closes; the seal happens on
            # its egress_ended webhook — surface the in-between state.
            row.recording_status = "processing"
            session.add(row)
        if row.status == "active":
            from case_service.db import repository as repo
            case = await repo.get_case_instance(session, row.case_id)
            await end_session(session, row=row,
                              case_type_id=case.case_type_id if case else None,
                              actor="livekit")
        elif row.recording_status == "processing":
            await session.commit()
        return

    identity = (event.get("participant") or {}).get("identity")
    if not identity:
        return
    participant = await _participant_by_identity(session, row.id, identity)
    if participant is None:
        participant = CaseSessionParticipantModel(
            session_id=row.id, tenant_id=row.tenant_id, identity=identity, role="guest",
        )
        session.add(participant)
    if kind == "participant_joined":
        participant.joined_at = _utcnow()
    else:
        participant.left_at = _utcnow()
    await session.commit()
    await _emit(row.case_id, f"meet.{kind}", {
        "session_id": str(row.id), "identity": identity,
    })


async def list_providers(session: AsyncSession, tenant_id: str) -> list[dict]:
    """Enabled meeting connectors for this tenant (Studio provider picker)."""
    rows = (await session.execute(
        select(ConnectorRegistryModel).where(
            ConnectorRegistryModel.connector_type.in_(MEETING_CONNECTOR_TYPES),
            ConnectorRegistryModel.enabled == True,  # noqa: E712
            or_(ConnectorRegistryModel.tenant_id == tenant_id,
                ConnectorRegistryModel.tenant_id.is_(None)),
        )
    )).scalars().all()
    cfg_default = (await tenant_meet_settings(session, tenant_id)).get("provider")
    return [
        {
            "connector_id": str(r.id),
            "name":         r.name,
            "provider":     _PROVIDER_BY_TYPE[r.connector_type],
            "is_default":   _PROVIDER_BY_TYPE[r.connector_type] == cfg_default,
        }
        for r in rows
    ]
