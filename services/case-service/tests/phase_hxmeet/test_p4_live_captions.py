"""HxMeet P4a-live — streaming captions (browser-stream MVP).

Pins: the caption state machine (leading silence never buffered, partial
cadence while speech continues, finalize on trailing silence and on the
max-utterance cap), room-pinned LiveKit token verification (uniform None on
every failure mode), the tenant live_captions opt-in (default OFF), and the
accelerator auto-detect override. The WebSocket transport itself is proven
live (httpx's ASGI test client speaks no WS); Whisper is mocked throughout —
no model weights in CI.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import numpy as np
import pytest

from case_service.config import get_settings
from case_service.db.models import (
    CaseSessionCaptionSegmentModel,
    CaseSessionModel,
)
from case_service.meet import asr, livekit
from case_service.meet import service as meet_svc
from sqlalchemy import select
from tests.conftest import create_case, deploy_case_type

pytestmark = pytest.mark.asyncio

MEET = "/api/v1/meet"

SAMPLES_250MS = asr.SAMPLE_RATE // 4


@pytest.fixture
def lk_cfg():
    s = get_settings()
    saved = (s.livekit_url, s.livekit_api_key, s.livekit_api_secret)
    s.livekit_url = "ws://127.0.0.1:7880"
    s.livekit_api_key = "lk_test_key"
    s.livekit_api_secret = "lk_test_secret_of_sufficient_length"
    yield
    s.livekit_url, s.livekit_api_key, s.livekit_api_secret = saved


def _speech(chunks: int = 1) -> bytes:
    """250 ms chunks of loud int16 sine — comfortably above the silence gate."""
    t = np.arange(SAMPLES_250MS * chunks)
    return (np.sin(t * 0.05) * 8000).astype(np.int16).tobytes()


def _silence(chunks: int = 1) -> bytes:
    return b"\x00" * (SAMPLES_250MS * 2 * chunks)


# ── CaptionStream state machine ──────────────────────────────────────────────

class TestCaptionStream:
    async def test_leading_silence_emits_nothing_and_never_transcribes(self):
        stream = asr.CaptionStream()
        with patch.object(asr, "transcribe_pcm16", new=AsyncMock()) as tr:
            for _ in range(20):  # 5s of room tone
                assert await stream.feed(_silence()) is None
            tr.assert_not_called()

    async def test_partial_after_cadence_then_final_on_trailing_silence(self):
        stream = asr.CaptionStream()
        with patch.object(asr, "transcribe_pcm16",
                          new=AsyncMock(return_value="ask not")) as tr:
            results = []
            for _ in range(5):  # 1.25s speech ≥ 1.2s partial cadence
                r = await stream.feed(_speech())
                if r:
                    results.append(r)
            assert results == [{"text": "ask not", "is_final": False}]

            for _ in range(4):  # 1.0s trailing silence ≥ 0.8s finalize
                r = await stream.feed(_silence())
                if r:
                    results.append(r)
            assert results[-1] == {"text": "ask not", "is_final": True}
            # partial + final = exactly two transcriptions
            assert tr.await_count == 2

    async def test_final_resets_the_utterance_window(self):
        stream = asr.CaptionStream()
        with patch.object(asr, "transcribe_pcm16", new=AsyncMock(return_value="x")):
            for _ in range(5):
                await stream.feed(_speech())
            for _ in range(4):
                await stream.feed(_silence())
        assert len(stream._buf) == 0
        assert stream._had_speech is False
        # post-final silence goes back to being dropped, not buffered
        assert await stream.feed(_silence()) is None
        assert len(stream._buf) == 0

    async def test_max_utterance_cap_forces_a_final(self):
        stream = asr.CaptionStream()
        finals = 0
        with patch.object(asr, "transcribe_pcm16", new=AsyncMock(return_value="long")):
            for _ in range(61):  # >15s of continuous speech
                r = await stream.feed(_speech())
                if r and r["is_final"]:
                    finals += 1
        assert finals == 1

    async def test_empty_transcription_is_swallowed(self):
        stream = asr.CaptionStream()
        with patch.object(asr, "transcribe_pcm16", new=AsyncMock(return_value="")):
            emitted = [await stream.feed(_speech()) for _ in range(6)]
        assert all(e is None for e in emitted)


# ── LiveKit access-token verification (room membership proof for the WS) ────

class TestVerifyAccessToken:
    def _mint(self, room: str, **overrides) -> str:
        s = get_settings()
        payload = {"iss": s.livekit_api_key, "sub": "user:t",
                   "nbf": int(time.time()) - 5, "exp": int(time.time()) + 60,
                   "video": {"room": room}, **overrides}
        return pyjwt.encode(payload, s.livekit_api_secret, algorithm="HS256")

    def test_valid_token_returns_claims(self, lk_cfg):
        claims = livekit.verify_access_token(self._mint("vx-default-r1"), room="vx-default-r1")
        assert claims and claims["sub"] == "user:t"

    def test_wrong_room_garbage_bad_issuer_all_uniformly_none(self, lk_cfg):
        assert livekit.verify_access_token(self._mint("vx-default-r1"), room="vx-default-OTHER") is None
        assert livekit.verify_access_token("garbage", room="vx-default-r1") is None
        assert livekit.verify_access_token(self._mint("vx-default-r1", iss="not-our-key"),
                                           room="vx-default-r1") is None

    def test_expired_token_none(self, lk_cfg):
        tok = self._mint("vx-default-r1", exp=int(time.time()) - 10)
        assert livekit.verify_access_token(tok, room="vx-default-r1") is None


# ── Tenant opt-in + accelerator detection ────────────────────────────────────

class TestGatesAndDetection:
    async def test_live_captions_default_off_then_on(self, session):
        from sqlalchemy import select
        from case_service.api.routers.meet import tenant_live_captions_enabled
        from case_service.db.models import TenantModel

        assert await tenant_live_captions_enabled(session, "default") is False
        tenant = (await session.execute(
            select(TenantModel).where(TenantModel.slug == "default"))).scalars().first()
        if tenant is None:
            tenant = TenantModel(slug="default", name="Default")
            session.add(tenant)
            await session.flush()
        tenant.settings = {**(tenant.settings or {}), "meet": {"live_captions": True}}
        session.add(tenant)
        await session.flush()
        assert await tenant_live_captions_enabled(session, "default") is True

    def test_backend_override_pins_engine(self):
        s = get_settings()
        original = s.meet_asr_backend
        try:
            for override, expected in [
                ("cuda",   ("faster-whisper", "cuda")),
                ("vulkan", ("whisper.cpp", "vulkan")),
                ("rocm",   ("whisper.cpp", "vulkan")),
                ("cpu",    ("faster-whisper", "cpu")),
            ]:
                s.meet_asr_backend = override
                assert asr.detect_backend() == expected
        finally:
            s.meet_asr_backend = original

    def test_auto_probes_the_machine(self):
        s = get_settings()
        original = s.meet_asr_backend
        try:
            s.meet_asr_backend = "auto"
            with patch.object(asr, "_has_nvidia", return_value=True):
                assert asr.detect_backend() == ("faster-whisper", "cuda")
            with patch.object(asr, "_has_nvidia", return_value=False), \
                 patch.object(asr, "_has_gpu_dri", return_value=True):
                assert asr.detect_backend() == ("whisper.cpp", "vulkan")
            with patch.object(asr, "_has_nvidia", return_value=False), \
                 patch.object(asr, "_has_gpu_dri", return_value=False):
                assert asr.detect_backend() == ("faster-whisper", "cpu")
        finally:
            s.meet_asr_backend = original


# ── P4a-live-2: sealed live transcript (the recording's twin) ────────────────

async def _active_session(session, client, *, record=True):
    ct = await deploy_case_type(client, name=f'LT CT {uuid.uuid4().hex[:6]}')
    case = await create_case(client, ct['id'])
    row = CaseSessionModel(
        case_id=uuid.UUID(case['id']), tenant_id="default", driver="embedded",
        provider="livekit", status="active", title="Live call",
        started_by="user:test-admin", record_intent=record,
        external_meeting_id=f"vx-default-{uuid.uuid4()}",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, ct


async def _staged_ended_session(session, client):
    """Active record-intent session with staged segments, then ended (seals)."""
    row, ct = await _active_session(session, client)
    await meet_svc.stage_caption_segment(session, row, speaker="user:test-admin",
                                         text="Hello, this is the live call.")
    await meet_svc.stage_caption_segment(session, row, speaker="email:guest@x.test",
                                         text="I confirm my date of birth.")
    row = await meet_svc.end_session(session, row=row, case_type_id=ct["id"],
                                     actor="user:test-admin")
    return row


class TestLiveTranscriptSeal:
    async def test_non_record_sessions_stage_nothing(self, session, client, lk_cfg):
        row, _ct = await _active_session(session, client, record=False)
        await meet_svc.stage_caption_segment(session, row, speaker="user:u", text="hi")
        n = (await session.execute(
            select(CaseSessionCaptionSegmentModel)
            .where(CaseSessionCaptionSegmentModel.session_id == row.id))).scalars().all()
        assert n == []

    async def test_ended_sessions_stage_nothing(self, session, client, lk_cfg):
        row, ct = await _active_session(session, client)
        await meet_svc.end_session(session, row=row, case_type_id=ct["id"], actor="user:u")
        await meet_svc.stage_caption_segment(session, row, speaker="user:u", text="late")
        n = (await session.execute(
            select(CaseSessionCaptionSegmentModel)
            .where(CaseSessionCaptionSegmentModel.session_id == row.id))).scalars().all()
        assert n == []

    async def test_end_seals_and_deletes_staging(self, session, client, lk_cfg):
        row = await _staged_ended_session(session, client)
        assert row.transcript_status == "sealed"
        assert row.transcript_document_id is not None
        assert "sha256:" in (row.transcript_anchor_ref or "")
        left = (await session.execute(
            select(CaseSessionCaptionSegmentModel)
            .where(CaseSessionCaptionSegmentModel.session_id == row.id))).scalars().all()
        assert left == []  # plaintext staging never outlives the session

        # ciphertext at rest: the stored doc must NOT contain the spoken text
        from case_service.documents.service import DocumentService
        data, name, _ct2 = await DocumentService().download(session, row.transcript_document_id)
        assert b"live call" not in data.lower()
        assert name.endswith(".hxsealed")

        from case_service.db.models import CaseAuditLogModel
        audit = (await session.execute(
            select(CaseAuditLogModel)
            .where(CaseAuditLogModel.action == "meet.transcript.sealed"))).scalars().all()
        assert any(a.details.get("session_id") == str(row.id) for a in audit)

    async def test_end_without_segments_stays_none(self, session, client, lk_cfg):
        row, ct = await _active_session(session, client)
        row = await meet_svc.end_session(session, row=row, case_type_id=ct["id"], actor="user:u")
        assert row.transcript_status == "none"
        assert row.transcript_document_id is None


class TestTranscriptEndpoints:
    async def test_view_returns_text_and_audits(self, session, client, lk_cfg):
        row = await _staged_ended_session(session, client)
        sid = str(row.id)  # expire_all() below would force a lazy refresh on row
        r = await client.get(f"{MEET}/sessions/{sid}/transcript")
        assert r.status_code == 200
        assert "Hello, this is the live call." in r.text
        assert "I confirm my date of birth." in r.text
        from case_service.db.models import CaseAuditLogModel
        # Shared-StaticPool hazard: a late teardown rollback can eat the view's
        # committed audit row — every view audits, so re-view until one sticks.
        for _ in range(3):
            session.expire_all()
            viewed = (await session.execute(
                select(CaseAuditLogModel)
                .where(CaseAuditLogModel.action == "transcript_viewed"))).scalars().all()
            if any(a.details.get("session_id") == sid for a in viewed):
                break
            await client.get(f"{MEET}/sessions/{sid}/transcript")
        else:
            pytest.fail("transcript_viewed audit row never persisted")

    async def test_no_transcript_404(self, session, client, lk_cfg):
        row, ct = await _active_session(session, client)
        row = await meet_svc.end_session(session, row=row, case_type_id=ct["id"], actor="user:u")
        r = await client.get(f"{MEET}/sessions/{row.id}/transcript")
        assert r.status_code == 404

    async def test_verify_confirms_hash(self, session, client, lk_cfg):
        row = await _staged_ended_session(session, client)
        r = await client.get(f"{MEET}/sessions/{row.id}/transcript/verify")
        body = r.json()
        assert body["verified"] is True
        assert body["sha256"] == body["sealed_sha256"]
        assert "chain-seq:" in body["anchor_ref"]

    async def test_tamper_detected(self, session, client, lk_cfg):
        row = await _staged_ended_session(session, client)
        from case_service.documents.service import DocumentService
        svc = DocumentService()
        data, _n, _c = await svc.download(session, row.transcript_document_id)
        await svc.overwrite(session, row.transcript_document_id, bytes([data[0] ^ 0xFF]) + data[1:])
        await session.commit()
        assert (await client.get(f"{MEET}/sessions/{row.id}/transcript")).status_code == 409
        v = await client.get(f"{MEET}/sessions/{row.id}/transcript/verify")
        assert v.json()["verified"] is False

    async def test_unauthenticated_401(self, session, anon_client, client, lk_cfg):
        row = await _staged_ended_session(session, client)
        r = await anon_client.get(f"{MEET}/sessions/{row.id}/transcript")
        assert r.status_code == 401


class TestIntelligenceReuse:
    async def test_analysis_reuses_sealed_transcript_without_whisper(self, session, client, lk_cfg):
        from case_service.db.models import CaseSessionIntelligenceModel, TenantModel
        from case_service.meet import intelligence as _intel

        tenant = (await session.execute(
            select(TenantModel).where(TenantModel.slug == "default"))).scalars().first()
        if tenant is None:
            tenant = TenantModel(slug="default", name="Default")
            session.add(tenant)
        tenant.settings = {**(tenant.settings or {}), "meet": {"intelligence": True}}
        session.add(tenant)
        await session.commit()

        row = await _staged_ended_session(session, client)
        session.add(CaseSessionIntelligenceModel(session_id=row.id, requested_by="user:test-admin"))
        await session.commit()

        def _boom(*a, **k):
            raise AssertionError("whisper must not run when a sealed live transcript exists")

        with patch.object(_intel, "_transcribe", _boom), \
             patch.object(_intel, "_summarize",
                          new=AsyncMock(return_value=("Live summary.", ["Do the thing"], "test-llm"))):
            await _intel.run_intelligence_job(row.id)

        body = (await client.get(f"{MEET}/sessions/{row.id}/intelligence")).json()
        assert body["status"] == "completed", body.get("error") or body
        assert body["summary"] == "Live summary."
        assert body["transcript_document_id"] == str(row.transcript_document_id)
        assert "live-captions" in body["model_versions"]["whisper"]
