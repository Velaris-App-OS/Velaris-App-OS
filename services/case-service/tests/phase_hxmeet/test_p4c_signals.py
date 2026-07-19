"""HxMeet P4c-2 — passive signal pass on the sealed recording.

Pins: tenant opt-in gate (400), sealed-evidence requirement (404), 501 only
when NO check could run, 409 while running, honest skips (missing deps and
missing transcript are recorded as skipped with the reason, never silently),
the deterministic phrase-readback cross-check (found / not-found / spoken
words), the heuristic scorers on synthetic frames, risk score = mean of the
checks that ran, per-tenant review threshold, assistive framing
(review_recommended, never a verdict), the meet.recording.view read gate and
the audit event.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy import select

from case_service.config import get_settings
from case_service.db.models import (
    CaseSessionChallengeModel,
    CaseSessionKycSignalsModel,
    CaseSessionModel,
    TenantModel,
)
from case_service.meet import kyc as meet_kyc
from tests.conftest import create_case, deploy_case_type

MEET = "/api/v1/meet"


@pytest_asyncio.fixture
async def kyc_cfg():
    s = get_settings()
    s.meet_driver = "embedded"
    s.livekit_url = "wss://livekit.test"
    s.livekit_api_key = "lk_test_key"
    s.livekit_api_secret = "lk_test_secret_of_sufficient_length"
    yield s


async def _enable_kyc(session, slug="default", threshold=None):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug))).scalars().first()
    if tenant is None:
        tenant = TenantModel(slug=slug, name=slug, settings={})
        session.add(tenant)
    settings = dict(tenant.settings or {})
    meet = {**settings.get("meet", {}), "kyc": True}
    if threshold is not None:
        meet["kyc_review_threshold"] = threshold
    settings["meet"] = meet
    tenant.settings = settings
    await session.commit()


async def _sealed_session(session, client, *, sealed_recording=True,
                          sealed_transcript=False, tenant_slug="default") -> CaseSessionModel:
    from case_service.documents.service import DocumentService
    ct = await deploy_case_type(client, name=f'P4c2 CT {uuid.uuid4().hex[:6]}')
    case = await create_case(client, ct['id'])
    case_id = uuid.UUID(case['id'])
    doc_id = None
    tdoc_id = None
    if sealed_recording:
        doc = await DocumentService().upload(
            session, case_id=case_id, filename="rec.mp4.hxsealed",
            data=b"sealed-placeholder", content_type="application/octet-stream",
            uploaded_by="hxmeet", tenant_id=tenant_slug)
        doc_id = doc.id
    if sealed_transcript:
        tdoc = await DocumentService().upload(
            session, case_id=case_id, filename="transcript.hxsealed",
            data=b"sealed-placeholder", content_type="application/octet-stream",
            uploaded_by="hxmeet", tenant_id=tenant_slug)
        tdoc_id = tdoc.id
    row = CaseSessionModel(
        case_id=case_id, tenant_id=tenant_slug, driver="embedded", provider="livekit",
        status="ended", title="KYC call", started_by="user:test-admin",
        record_intent=True,
        recording_status="sealed" if sealed_recording else "none",
        recording_document_id=doc_id,
        transcript_status="sealed" if sealed_transcript else "none",
        transcript_document_id=tdoc_id,
        external_meeting_id=f"vx-{tenant_slug}-{uuid.uuid4()}",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _frames(kind: str, n: int = 6):
    """Synthetic grayscale frames: 'static' = identical flat frames,
    'live' = flat frames with small random jitter, 'grid' = a hard pixel
    grid whose spectrum has isolated periodic peaks (screen-door)."""
    import numpy as np
    rng = np.random.default_rng(42)
    base = np.full((120, 160), 128, dtype=np.uint8)
    if kind == "static":
        return [base.copy() for _ in range(n)]
    if kind == "live":
        return [np.clip(base.astype(np.int16) + rng.integers(-6, 7, base.shape),
                        0, 255).astype(np.uint8) for _ in range(n)]
    if kind == "grid":
        f = base.copy()
        f[::4, :] = 255
        f[:, ::4] = 255
        return [f.copy() for _ in range(n)]
    raise ValueError(kind)


class TestScorers:
    def test_static_frames_score_photo_like(self):
        check = meet_kyc._micro_movement_check(_frames("static"))
        assert not check.get("skipped")
        assert check["score"] == 1.0     # zero motion = photo-like

    def test_jittering_frames_score_alive(self):
        check = meet_kyc._micro_movement_check(_frames("live"))
        assert check["score"] < 0.5

    def test_grid_frames_flag_screen_replay(self):
        clean = meet_kyc._screen_replay_check(_frames("live"))
        screened = meet_kyc._screen_replay_check(_frames("grid"))
        assert screened["score"] > clean["score"]

    def test_too_few_frames_skip_honestly(self):
        assert meet_kyc._micro_movement_check([])["skipped"] is True
        assert meet_kyc._screen_replay_check([])["skipped"] is True


class TestPhraseCrossCheck:
    def _challenge(self, phrase: str) -> CaseSessionChallengeModel:
        return CaseSessionChallengeModel(
            id=uuid.uuid4(), session_id=uuid.uuid4(), kind="phrase_readback",
            payload={"phrase": phrase}, issued_by="u")

    def test_digits_found(self):
        out = meet_kyc._phrase_readback_checks(
            [self._challenge("4 9 3 2 1 7")],
            "[00:12] worker: please read\n[00:15] guest: 4 9 3 2 1 7 okay")
        assert out[0]["score"] == 0.0

    def test_spoken_words_found(self):
        out = meet_kyc._phrase_readback_checks(
            [self._challenge("4 9 3 2 1 7")],
            "guest: four nine three two one seven")
        assert out[0]["score"] == 0.0

    def test_digits_missing(self):
        out = meet_kyc._phrase_readback_checks(
            [self._challenge("4 9 3 2 1 7")], "guest: hello there")
        assert out[0]["score"] == 1.0

    def test_no_transcript_skips(self):
        out = meet_kyc._phrase_readback_checks([self._challenge("1 2 3 4 5 6")], None)
        assert out[0]["skipped"] is True

    def test_no_phrase_challenges_no_checks(self):
        assert meet_kyc._phrase_readback_checks([], "anything") == []


class TestKycAnalysisGates:
    async def test_tenant_gate_off_400(self, client, session, kyc_cfg):
        row = await _sealed_session(session, client)
        r = await client.post(f"{MEET}/sessions/{row.id}/kyc-analysis")
        assert r.status_code == 400

    async def test_no_sealed_evidence_404(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _sealed_session(session, client, sealed_recording=False)
        r = await client.post(f"{MEET}/sessions/{row.id}/kyc-analysis")
        assert r.status_code == 404

    async def test_nothing_runnable_501(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _sealed_session(session, client)   # recording, no transcript
        with patch("case_service.meet.kyc.cv_available", return_value=False):
            r = await client.post(f"{MEET}/sessions/{row.id}/kyc-analysis")
        assert r.status_code == 501

    async def test_transcript_only_runs_without_cv(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _sealed_session(session, client,
                                    sealed_recording=False, sealed_transcript=True)
        with patch("case_service.meet.kyc.cv_available", return_value=False):
            r = await client.post(f"{MEET}/sessions/{row.id}/kyc-analysis")
        assert r.status_code == 202

    async def test_running_409(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _sealed_session(session, client)
        session.add(CaseSessionKycSignalsModel(
            session_id=row.id, status="running", requested_by="u"))
        await session.commit()
        r = await client.post(f"{MEET}/sessions/{row.id}/kyc-analysis")
        assert r.status_code == 409


class TestKycJob:
    async def _run(self, client, session, row, transcript=None, challenges=()):
        for phrase in challenges:
            session.add(CaseSessionChallengeModel(
                session_id=row.id, tenant_id=row.tenant_id, kind="phrase_readback",
                payload={"phrase": phrase}, issued_by="user:test-admin"))
        session.add(CaseSessionKycSignalsModel(session_id=row.id, requested_by="user:test-admin"))
        await session.commit()
        with patch("case_service.meet.intelligence._unseal_recording",
                   new=AsyncMock(return_value=b"fake-mp4")), \
             patch("case_service.meet.kyc._decode_frames",
                   return_value=_frames("live")), \
             patch("case_service.meet.kyc._unseal_transcript_text",
                   new=AsyncMock(return_value=transcript)):
            await meet_kyc.run_kyc_job(row.id)
        return (await client.get(f"{MEET}/sessions/{row.id}/kyc-analysis")).json()

    async def test_full_run_scores_and_audits(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _sealed_session(session, client, sealed_transcript=True)
        body = await self._run(client, session, row,
                               transcript="guest: 1 2 3 4 5 6", challenges=["1 2 3 4 5 6"])
        assert body["status"] == "completed", body.get("error") or body
        assert body["risk_score"] is not None
        ran = {c["name"] for c in body["checks"] if not c.get("skipped")}
        assert ran == {"screen_replay", "micro_movement"}
        skipped = {c["name"] for c in body["checks"] if c.get("skipped")}
        # Honest skips, named: P4c model placeholders + the P4d checks whose
        # own gates (opt-in / consent / model) aren't met in this test.
        assert skipped == {"lip_sync", "audio_spoof", "face_match",
                           "gan_artifact", "document_match"}
        assert body["challenge_checks"][0]["score"] == 0.0
        assert "numpy" in body["model_versions"]

        from case_service.db.models import CaseAuditLogModel
        audit = (await session.execute(
            select(CaseAuditLogModel)
            .where(CaseAuditLogModel.action == "kyc_signals_analyzed")
        )).scalars().first()
        assert audit is not None
        assert audit.details["session_id"] == str(row.id)

    async def test_failed_phrase_raises_risk(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _sealed_session(session, client, sealed_transcript=True)
        matched = await self._run(client, session, row,
                                  transcript="guest: 9 9 9 9 9 9", challenges=["9 9 9 9 9 9"])
        row2 = await _sealed_session(session, client, sealed_transcript=True)
        missed = await self._run(client, session, row2,
                                 transcript="guest: silence", challenges=["1 2 3 4 5 6"])
        assert missed["risk_score"] > matched["risk_score"]
        assert missed["challenge_checks"][0]["score"] == 1.0

    async def test_review_threshold_is_tenant_config(self, client, session, kyc_cfg):
        await _enable_kyc(session, threshold=0.05)
        row = await _sealed_session(session, client, sealed_transcript=True)
        body = await self._run(client, session, row,
                               transcript="guest: nope", challenges=["1 2 3 4 5 6"])
        assert body["review_threshold"] == 0.05
        assert body["review_recommended"] is True       # missed phrase over a low bar

    async def test_no_transcript_skips_cross_check(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _sealed_session(session, client)   # no sealed transcript
        body = await self._run(client, session, row,
                               transcript=None, challenges=["1 2 3 4 5 6"])
        assert body["status"] == "completed"
        assert body["challenge_checks"][0]["skipped"] is True

    async def test_get_without_run_returns_none_status(self, client, session, kyc_cfg):
        row = await _sealed_session(session, client)
        body = (await client.get(f"{MEET}/sessions/{row.id}/kyc-analysis")).json()
        assert body["status"] == "none"

    async def test_read_gate_is_recording_view(self, client, session, kyc_cfg):
        """Enforce mode: reading the analysis derives from the recording —
        an unrelated user gets the 404 anti-oracle."""
        from case_service.auth.jwt_handler import create_dev_token
        await _enable_kyc(session)
        row = await _sealed_session(session, client)
        s = get_settings()
        s.hxguard_case_enforcement = "enforce"
        assert (await client.get(f"{MEET}/sessions/{row.id}/kyc-analysis")).status_code == 200

        outsider = create_dev_token(
            user_id=str(uuid.uuid4()), username="outsider", roles=["viewer"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
        )
        r = await client.get(f"{MEET}/sessions/{row.id}/kyc-analysis",
                             headers={"Authorization": f"Bearer {outsider}"})
        assert r.status_code == 404
