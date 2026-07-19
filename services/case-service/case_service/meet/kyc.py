"""HxMeet P4c — Video-KYC liveness challenges (+ passive signal pass, P4c-2).

Randomized challenge–response is the backbone of the KYC pipeline: generative
attacks are strongest against predictable, passive footage and weakest against
unscripted real-time interaction. Challenges are minted SERVER-SIDE with a
CSPRNG (the far end can never predict them), issued only while a record-intent
session is actually recording — so instruction and response both land in the
sealed recording — and the observed result is recorded by the WORKER. The AI
never auto-passes anything.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseSessionChallengeModel,
    CaseSessionModel,
    TenantModel,
)

CHALLENGE_KINDS = ("head_turn", "phrase_readback", "document_tilt")
CHALLENGE_RESULTS = ("passed", "failed", "skipped")

_DIRECTIONS = ("left", "right", "up")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def tenant_kyc_enabled(session: AsyncSession, tenant_slug: str | None) -> bool:
    """Per-tenant opt-in, default OFF — KYC analysis is a policy decision
    even though every model runs locally."""
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (tenant_slug or "default"))
    )).scalars().first()
    return bool(((tenant.settings or {}).get("meet", {}) if tenant else {}).get("kyc", False))


async def tenant_kyc_biometrics_enabled(session: AsyncSession, tenant_slug: str | None) -> bool:
    """P4d — a SECOND opt-in on top of `kyc`, default OFF: biometric matching
    is GDPR Art. 9 territory and never rides along implicitly."""
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (tenant_slug or "default"))
    )).scalars().first()
    meet = (tenant.settings or {}).get("meet", {}) if tenant else {}
    return bool(meet.get("kyc", False)) and bool(meet.get("kyc_biometrics", False))


def _mint_payload(kind: str) -> dict:
    if kind == "head_turn":
        sequence = [secrets.choice(_DIRECTIONS) for _ in range(3)]
        return {
            "sequence": sequence,
            "instruction": "Turn your head " + ", then ".join(sequence) + ".",
        }
    if kind == "phrase_readback":
        phrase = " ".join(secrets.choice("0123456789") for _ in range(6))
        return {
            "phrase": phrase,
            "instruction": f"Please read these digits aloud: {phrase}",
        }
    if kind == "document_tilt":
        side = secrets.choice(("left", "right"))
        return {
            "side": side,
            "instruction": (
                "Hold your ID next to your face, then slowly tilt it to the "
                f"{side} so the surface catches the light."
            ),
        }
    raise ValueError(f"Unknown challenge kind: {kind}")


async def issue_challenges(
    session: AsyncSession,
    *,
    row: CaseSessionModel,
    kinds: list[str],
    actor: str,
) -> list[CaseSessionChallengeModel]:
    """Mint one randomized challenge per requested kind. Caller has already
    enforced the gates (record-intent, recording running, tenant opt-in)."""
    from case_service.api.routers.cases import _audit

    challenges = [
        CaseSessionChallengeModel(
            session_id=row.id,
            tenant_id=row.tenant_id,
            kind=kind,
            payload=_mint_payload(kind),
            issued_by=actor,
        )
        for kind in kinds
    ]
    session.add_all(challenges)
    await _audit(session, row.case_id, "kyc_challenges_issued", actor_id=actor,
                 details={"session_id": str(row.id), "kinds": kinds})
    await session.commit()
    for c in challenges:
        await session.refresh(c)
    return challenges


async def list_challenges(
    session: AsyncSession, session_id: uuid.UUID,
) -> list[CaseSessionChallengeModel]:
    return list((await session.execute(
        select(CaseSessionChallengeModel)
        .where(CaseSessionChallengeModel.session_id == session_id)
        .order_by(CaseSessionChallengeModel.issued_at)
    )).scalars().all())


async def record_challenge_result(
    session: AsyncSession,
    *,
    row: CaseSessionModel,
    challenge_id: str,
    result: str,
    notes: str | None,
    actor: str,
) -> CaseSessionChallengeModel | None:
    """The worker's observed verdict for one challenge — human judgment,
    recorded. Returns None when the challenge doesn't belong to the session
    (the router turns that into a uniform 404)."""
    from case_service.api.routers.cases import _audit

    try:
        cid = uuid.UUID(challenge_id)
    except ValueError:
        return None
    challenge = await session.get(CaseSessionChallengeModel, cid)
    if challenge is None or challenge.session_id != row.id:
        return None
    challenge.result = result
    challenge.result_notes = notes
    challenge.result_by = actor
    challenge.result_at = _utcnow()
    await _audit(session, row.case_id, "kyc_challenge_result", actor_id=actor,
                 details={"session_id": str(row.id), "challenge_id": str(challenge.id),
                          "kind": challenge.kind, "result": result})
    await session.commit()
    await session.refresh(challenge)
    return challenge


def challenge_view(c: CaseSessionChallengeModel) -> dict:
    return {
        "id":           str(c.id),
        "session_id":   str(c.session_id),
        "kind":         c.kind,
        "payload":      c.payload or {},
        "issued_by":    c.issued_by,
        "issued_at":    c.issued_at.isoformat() if c.issued_at else None,
        "result":       c.result,
        "result_notes": c.result_notes,
        "result_by":    c.result_by,
        "result_at":    c.result_at.isoformat() if c.result_at else None,
    }


# ═══ P4c-2 — passive signal pass (post-session, on the SEALED recording) ═══
#
# Every check emits a 0..1 risk contribution (higher = riskier) OR is skipped
# honestly with the reason recorded. The final risk_score is the mean of the
# checks that actually ran — labelled assistive everywhere; classifiers decay
# as generators improve, which is why the challenge cross-check (deterministic)
# and the human verdict carry the pipeline.

def cv_available() -> bool:
    """The frame-analysis checks need av (in-memory MP4 decode) + numpy."""
    try:
        import av      # noqa: F401
        import numpy   # noqa: F401
        return True
    except ImportError:
        return False


def _decode_frames(mp4_bytes: bytes, max_frames: int = 16) -> list:
    """Sample up to max_frames grayscale frames, decoding from memory —
    plaintext video never touches disk (same posture as P4a)."""
    import io

    import av
    frames = []
    with av.open(io.BytesIO(mp4_bytes)) as container:
        stream = container.streams.video[0]
        total = stream.frames or 0
        step = max(1, total // max_frames if total else 10)
        for i, frame in enumerate(container.decode(stream)):
            if i % step == 0:
                frames.append(frame.to_ndarray(format="gray"))
            if len(frames) >= max_frames:
                break
    return frames


def _screen_replay_check(frames: list) -> dict:
    """Moiré / screen-door heuristic: a screen filmed by a camera aliases into
    strong isolated mid/high-frequency peaks in the spectrum; optical scenes
    don't. Physical artifact — the most robust passive signal we have."""
    import numpy as np
    if not frames:
        return {"name": "screen_replay", "skipped": True,
                "detail": "no video frames decoded"}
    ratios = []
    for f in frames:
        h, w = f.shape
        crop = f[h // 4: 3 * h // 4, w // 4: 3 * w // 4].astype(np.float32)
        win = np.hanning(crop.shape[0])[:, None] * np.hanning(crop.shape[1])[None, :]
        spec = np.abs(np.fft.fftshift(np.fft.fft2(crop * win)))
        spec /= spec.max() + 1e-9
        ch, cw = spec.shape[0] // 2, spec.shape[1] // 2
        spec[ch - 8: ch + 8, cw - 8: cw + 8] = 0.0   # drop DC / natural low-freq
        peak = float(spec.max())
        med = float(np.median(spec[spec > 0])) + 1e-9
        ratios.append(peak / med)
    ratio = float(np.mean(ratios))
    # Heuristic mapping: optical footage typically lands well under ~100;
    # screened content produces isolated peaks orders of magnitude over median.
    score = min(1.0, max(0.0, (ratio - 100.0) / 900.0))
    return {"name": "screen_replay", "score": round(score, 3),
            "detail": f"spectral peak/median ratio {ratio:.0f} across {len(frames)} frames (heuristic)",
            "model": "fft-moire-heuristic/1"}


def _micro_movement_check(frames: list) -> dict:
    """Temporal micro-motion statistics: live faces move continuously at small
    amplitude; a static photo (or paused replay) is unnaturally still. This is
    a statistic, not a liveness verdict."""
    import numpy as np
    if len(frames) < 2:
        return {"name": "micro_movement", "skipped": True,
                "detail": "fewer than 2 frames decoded"}
    diffs = [float(np.mean(np.abs(frames[i + 1].astype(np.float32) - frames[i].astype(np.float32))))
             for i in range(len(frames) - 1)]
    motion = float(np.mean(diffs))
    # Near-zero inter-frame change across the sampled span = photo-like.
    score = 1.0 if motion < 0.3 else max(0.0, 1.0 - motion / 3.0)
    return {"name": "micro_movement", "score": round(score, 3),
            "detail": f"mean inter-frame change {motion:.2f} across {len(frames)} sampled frames (statistic)",
            "model": "frame-diff-stats/1"}


def _phrase_readback_checks(challenges: list[CaseSessionChallengeModel],
                            transcript: str | None) -> list[dict]:
    """Deterministic cross-check: the digits the server minted must appear in
    the sealed live transcript. This is the check that stays strong as
    generators improve — the phrase didn't exist before the session."""
    phrase_challenges = [c for c in challenges if c.kind == "phrase_readback"]
    if not phrase_challenges:
        return []
    if transcript is None:
        return [{"name": "phrase_readback", "skipped": True,
                 "detail": "no sealed live transcript to cross-check against"}]
    # Normalize: digits only, with spoken numbers mapped ("four" > 4).
    words_to_digits = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
                       "four": "4", "five": "5", "six": "6", "seven": "7",
                       "eight": "8", "nine": "9"}
    normalized = transcript.lower()
    for word, digit in words_to_digits.items():
        normalized = normalized.replace(word, digit)
    digit_stream = "".join(ch for ch in normalized if ch.isdigit())
    out = []
    for c in phrase_challenges:
        expected = "".join((c.payload or {}).get("phrase", "").split())
        found = bool(expected) and expected in digit_stream
        out.append({"name": "phrase_readback",
                    "challenge_id": str(c.id),
                    "score": 0.0 if found else 1.0,
                    "detail": ("minted digits found in the sealed transcript" if found
                               else "minted digits NOT found in the sealed transcript"),
                    "model": "deterministic-transcript-match/1"})
    return out


_UNAVAILABLE_CHECKS = (
    ("lip_sync", "audio-visual sync model not installed — install a local "
                 "SyncNet-class model to enable"),
    ("audio_spoof", "audio anti-spoofing model not installed — install a local "
                    "AASIST-class model to enable"),
    # P4d placeholders — named now so the breakdown is honest about what a
    # full pipeline would include.
    ("gan_artifact", "deepfake-artifact classifier not installed — install a "
                     "local GAN-artifact model to enable"),
    ("document_match", "document localization model not installed — live-vs-"
                       "uploaded document comparison needs it"),
)


# ── P4d — biometric face match (strictly opt-in, compare-and-discard) ──

def face_available() -> bool:
    try:
        import insightface  # noqa: F401
        return True
    except ImportError:
        return False


_FACE_ANALYZER = None


def _face_analyzer():
    """One lazily-built InsightFace analyzer per process (model load is slow)."""
    global _FACE_ANALYZER
    if _FACE_ANALYZER is None:
        from insightface.app import FaceAnalysis
        analyzer = FaceAnalysis(providers=["CPUExecutionProvider"])
        analyzer.prepare(ctx_id=-1)
        _FACE_ANALYZER = analyzer
    return _FACE_ANALYZER


def _largest_face(faces):
    return max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))


def _embed_image_bytes(image_bytes: bytes):
    """Face embedding of the largest face in a still image (the verified ID).
    Returns None when no face is found. The embedding exists only in memory."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    faces = _face_analyzer().get(img)
    return _largest_face(faces).embedding if faces else None


def _embed_frames(frames: list):
    """Face embeddings across sampled session frames (grayscale > BGR)."""
    import cv2
    analyzer = _face_analyzer()
    out = []
    for f in frames:
        faces = analyzer.get(cv2.cvtColor(f, cv2.COLOR_GRAY2BGR))
        if faces:
            out.append(_largest_face(faces).embedding)
    return out


async def _verified_id_document(session: AsyncSession, case_id: uuid.UUID):
    """The most recent PASSED document verification's document for this case —
    the P4b gate gives the video stage its ground truth."""
    from case_service.db.models import DocumentVerificationModel
    return (await session.execute(
        select(DocumentVerificationModel)
        .where(DocumentVerificationModel.case_id == case_id,
               DocumentVerificationModel.status == "passed")
        .order_by(DocumentVerificationModel.created_at.desc())
    )).scalars().first()


async def _face_match_check(session: AsyncSession, row: CaseSessionModel,
                            frames: list) -> dict:
    """Face-on-ID vs face-in-session. Every gate short-circuits into an honest
    skip; embeddings are computed, compared, and DISCARDED — only the score
    (and the fact of comparison) is stored."""
    import asyncio

    from case_service.db.models import CaseSessionParticipantModel

    name = "face_match"
    if not await tenant_kyc_biometrics_enabled(session, row.tenant_id):
        return {"name": name, "skipped": True,
                "detail": "biometric matching is not enabled for this tenant"}
    unconsented = (await session.execute(
        select(CaseSessionParticipantModel.identity)
        .where(CaseSessionParticipantModel.session_id == row.id,
               CaseSessionParticipantModel.joined_at.is_not(None),
               CaseSessionParticipantModel.biometric_consent_at.is_(None))
    )).scalars().all()
    if unconsented:
        return {"name": name, "skipped": True,
                "detail": f"participant(s) without biometric consent: {', '.join(unconsented)}"}
    if not face_available():
        return {"name": name, "skipped": True,
                "detail": "face-embedding model not installed (pip install insightface)"}
    if not frames:
        return {"name": name, "skipped": True,
                "detail": "no video frames decoded from the sealed recording"}
    verification = await _verified_id_document(session, row.case_id)
    if verification is None:
        return {"name": name, "skipped": True,
                "detail": "no passed document verification on this case to match against"}

    from case_service.documents.service import DocumentService
    doc_bytes, _name, _ct = await DocumentService().download(session, verification.document_id)
    id_embedding = await asyncio.to_thread(_embed_image_bytes, doc_bytes)
    if id_embedding is None:
        return {"name": name, "skipped": True,
                "detail": "no face detected on the verified ID document"}
    frame_embeddings = await asyncio.to_thread(_embed_frames, frames)
    if not frame_embeddings:
        return {"name": name, "skipped": True,
                "detail": "no face detected in the sampled session frames"}

    import numpy as np
    def _cos(a, b) -> float:
        return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))
    best = max(_cos(id_embedding, e) for e in frame_embeddings)
    del id_embedding, frame_embeddings   # compare-and-discard — scores only
    # InsightFace cosine similarity: same person typically ≥ ~0.35–0.4.
    score = min(1.0, max(0.0, (0.4 - best) / 0.4))
    return {"name": name, "score": round(score, 3),
            "detail": (f"best face similarity {best:.2f} vs the verified ID "
                       "(embeddings compared in memory and discarded)"),
            "model": "insightface-cosine/1"}


async def _unseal_transcript_text(session: AsyncSession, row: CaseSessionModel) -> str | None:
    if row.transcript_status != "sealed" or not row.transcript_document_id:
        return None
    from case_service.documents.service import DocumentService
    from case_service.hxvault import crypto as vault
    from case_service.hxvault.keyring import ensure_dek
    from case_service.meet import service as meet_svc

    sealed, _n, _ct = await DocumentService().download(session, row.transcript_document_id)
    tenant_row = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (row.tenant_id or "default"))
    )).scalars().first()
    dek = await ensure_dek(session, tenant_row.id if tenant_row else None)
    return vault.open_(dek, sealed, f"{meet_svc.TRANSCRIPT_AAD_PREFIX}{row.id}".encode()).decode()


async def run_kyc_job(session_id: uuid.UUID) -> None:
    """Background job — owns its own DB session (the request one is gone)."""
    import asyncio
    import logging

    from case_service.api.routers.cases import _audit
    from case_service.db.models import CaseSessionKycSignalsModel
    from case_service.db.session import get_session_factory
    from case_service.meet import intelligence as meet_intel

    log = logging.getLogger(__name__)
    factory = get_session_factory()
    async with factory() as session:
        signals = await session.get(CaseSessionKycSignalsModel, session_id)
        row = await session.get(CaseSessionModel, session_id)
        if signals is None or row is None:
            return
        signals.status = "running"
        await session.commit()

        try:
            checks: list[dict] = []
            model_versions: dict = {}
            frames: list = []

            if row.recording_status == "sealed" and row.recording_document_id and cv_available():
                import av
                import numpy
                plaintext = await meet_intel._unseal_recording(session, row)
                frames = await asyncio.to_thread(_decode_frames, plaintext)
                del plaintext
                checks.append(await asyncio.to_thread(_screen_replay_check, frames))
                checks.append(await asyncio.to_thread(_micro_movement_check, frames))
                model_versions["av"] = getattr(av, "__version__", "unknown")
                model_versions["numpy"] = numpy.__version__
            else:
                reason = ("frame analysis unavailable — install .[kyc] (av + numpy)"
                          if not cv_available() else "no sealed recording")
                checks.append({"name": "screen_replay", "skipped": True, "detail": reason})
                checks.append({"name": "micro_movement", "skipped": True, "detail": reason})

            # P4d: biometric face match — its own opt-in + consent ladder,
            # every unmet gate becomes an honest skip.
            checks.append(await _face_match_check(session, row, frames))

            for name, detail in _UNAVAILABLE_CHECKS:
                checks.append({"name": name, "skipped": True, "detail": detail})

            transcript = await _unseal_transcript_text(session, row)
            challenge_checks = _phrase_readback_checks(
                await list_challenges(session, row.id), transcript)

            scored = [c["score"] for c in checks + challenge_checks
                      if not c.get("skipped") and c.get("score") is not None]
            risk_score = round(sum(scored) / len(scored), 3) if scored else None

            signals.status = "completed"
            signals.risk_score = risk_score
            signals.checks = checks
            signals.challenge_checks = challenge_checks
            signals.model_versions = model_versions
            signals.error = None
            signals.completed_at = _utcnow()
            await _audit(session, row.case_id, "kyc_signals_analyzed",
                         actor_id=signals.requested_by,
                         details={"session_id": str(row.id),
                                  "risk_score": risk_score,
                                  "checks_run": len(scored)})
            await session.commit()
        except Exception as exc:
            log.warning("KYC signal pass failed for %s: %s", session_id, exc)
            await session.rollback()
            signals = await session.get(CaseSessionKycSignalsModel, session_id)
            if signals is not None:
                signals.status = "failed"
                signals.error = str(exc)[:500]
                signals.completed_at = _utcnow()
                await session.commit()


async def tenant_review_threshold(session: AsyncSession, tenant_slug: str | None) -> float:
    """Per-tenant score-above-which-review-is-recommended — a bank and a
    helpdesk have different risk appetites."""
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (tenant_slug or "default"))
    )).scalars().first()
    raw = ((tenant.settings or {}).get("meet", {}) if tenant else {}).get("kyc_review_threshold", 0.5)
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.5


def kyc_view(signals, threshold: float) -> dict:
    if signals is None:
        return {"status": "none"}
    return {
        "status":           signals.status,
        "risk_score":       signals.risk_score,
        # Assistive framing, never a verdict: a low score is NOT "verified".
        "review_recommended": (None if signals.risk_score is None
                               else signals.risk_score >= threshold),
        "review_threshold": threshold,
        "checks":           signals.checks or [],
        "challenge_checks": signals.challenge_checks or [],
        "model_versions":   signals.model_versions or {},
        "error":            signals.error,
        "requested_by":     signals.requested_by,
        "completed_at":     signals.completed_at.isoformat() if signals.completed_at else None,
    }
