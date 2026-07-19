"""HxMeet P4d — biometric cross-match (strictly opt-in, compare-and-discard).

Pins: the SECOND opt-in (`kyc_biometrics` never rides along with `kyc`), the
distinct biometric consent stamp on every join path (worker token mint, guest
exchange) — stamped ONLY when the tenant opted in so the UI showed the
distinct notice first, the guest preview announcing `biometric_notice`, the
face_match skip ladder (opt-in off > unconsented participants > model missing
> no frames > no verified ID), the scored path with a mocked embedder, and
the compare-and-discard invariant: no embedding is ever stored, only scores.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy import select

from case_service.config import get_settings
from case_service.db.models import (
    CaseSessionKycSignalsModel,
    CaseSessionModel,
    CaseSessionParticipantModel,
    DocumentVerificationModel,
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


async def _configure(session, *, kyc=True, biometrics=True, slug="default"):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug))).scalars().first()
    if tenant is None:
        tenant = TenantModel(slug=slug, name=slug, settings={})
        session.add(tenant)
    settings = dict(tenant.settings or {})
    settings["meet"] = {**settings.get("meet", {}),
                        "kyc": kyc, "kyc_biometrics": biometrics}
    tenant.settings = settings
    await session.commit()


async def _active_session(session, client, *, record_intent=True) -> CaseSessionModel:
    ct = await deploy_case_type(client, name=f'P4d CT {uuid.uuid4().hex[:6]}')
    case = await create_case(client, ct['id'])
    row = CaseSessionModel(
        case_id=uuid.UUID(case['id']), tenant_id="default", driver="embedded",
        provider="livekit", status="active", title="KYC call",
        started_by="user:test-admin", record_intent=record_intent,
        external_meeting_id=f"vx-default-{uuid.uuid4()}",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _joined(row, identity="customer:guest", *, biometric=False):
    now = datetime.now(timezone.utc)
    return CaseSessionParticipantModel(
        session_id=row.id, tenant_id=row.tenant_id, identity=identity,
        role="guest", joined_at=now,
        consent_recorded_at=now,
        biometric_consent_at=now if biometric else None,
    )


class TestBiometricConsentStamp:
    async def test_worker_token_stamps_when_opted_in(self, client, session, kyc_cfg):
        await _configure(session)
        row = await _active_session(session, client)
        r = await client.post(f"{MEET}/sessions/{row.id}/token")
        assert r.status_code == 200, r.text
        p = (await session.execute(
            select(CaseSessionParticipantModel)
            .where(CaseSessionParticipantModel.session_id == row.id)
        )).scalars().first()
        assert p.consent_recorded_at is not None
        assert p.biometric_consent_at is not None

    async def test_no_stamp_without_the_second_opt_in(self, client, session, kyc_cfg):
        await _configure(session, biometrics=False)   # kyc on, biometrics OFF
        row = await _active_session(session, client)
        await client.post(f"{MEET}/sessions/{row.id}/token")
        p = (await session.execute(
            select(CaseSessionParticipantModel)
            .where(CaseSessionParticipantModel.session_id == row.id)
        )).scalars().first()
        assert p.consent_recorded_at is not None      # recording consent as before
        assert p.biometric_consent_at is None         # Art. 9 never rides along

    async def test_no_stamp_without_record_intent(self, client, session, kyc_cfg):
        await _configure(session)
        row = await _active_session(session, client, record_intent=False)
        await client.post(f"{MEET}/sessions/{row.id}/token")
        p = (await session.execute(
            select(CaseSessionParticipantModel)
            .where(CaseSessionParticipantModel.session_id == row.id)
        )).scalars().first()
        assert p.biometric_consent_at is None

    async def test_biometrics_flag_requires_kyc_flag(self, client, session, kyc_cfg):
        await _configure(session, kyc=False, biometrics=True)
        assert await meet_kyc.tenant_kyc_biometrics_enabled(session, "default") is False

    async def test_guest_exchange_stamps_and_preview_announces(self, client, session, kyc_cfg):
        await _configure(session)
        row = await _active_session(session, client)
        invite = await client.post(f"{MEET}/sessions/{row.id}/invites",
                                   json={"email": "guest@example.com"})
        assert invite.status_code == 201, invite.text
        raw = invite.json()["invite_token"]

        preview = await client.post(f"{MEET}/guest/preview", json={"invite_token": raw})
        assert preview.status_code == 200
        assert preview.json()["biometric_notice"] is True

        r = await client.post(f"{MEET}/guest/token", json={"invite_token": raw})
        assert r.status_code == 200, r.text
        p = (await session.execute(
            select(CaseSessionParticipantModel)
            .where(CaseSessionParticipantModel.session_id == row.id,
                   CaseSessionParticipantModel.identity == "email:guest@example.com")
        )).scalars().first()
        assert p.biometric_consent_at is not None

    async def test_preview_quiet_when_biometrics_off(self, client, session, kyc_cfg):
        await _configure(session, biometrics=False)
        row = await _active_session(session, client)
        invite = await client.post(f"{MEET}/sessions/{row.id}/invites",
                                   json={"email": "guest2@example.com"})
        raw = invite.json()["invite_token"]
        preview = await client.post(f"{MEET}/guest/preview", json={"invite_token": raw})
        assert preview.json()["biometric_notice"] is False


class TestFaceMatchSkipLadder:
    async def test_opt_in_off_skips(self, client, session, kyc_cfg):
        await _configure(session, biometrics=False)
        row = await _active_session(session, client)
        check = await meet_kyc._face_match_check(session, row, [object()])
        assert check["skipped"] is True and "not enabled" in check["detail"]

    async def test_unconsented_participant_skips(self, client, session, kyc_cfg):
        await _configure(session)
        row = await _active_session(session, client)
        session.add(_joined(row, biometric=False))
        await session.commit()
        check = await meet_kyc._face_match_check(session, row, [object()])
        assert check["skipped"] is True
        assert "without biometric consent" in check["detail"]
        assert "customer:guest" in check["detail"]

    async def test_model_missing_skips(self, client, session, kyc_cfg):
        await _configure(session)
        row = await _active_session(session, client)
        session.add(_joined(row, biometric=True))
        await session.commit()
        with patch("case_service.meet.kyc.face_available", return_value=False):
            check = await meet_kyc._face_match_check(session, row, [object()])
        assert check["skipped"] is True and "not installed" in check["detail"]

    async def test_no_frames_skips(self, client, session, kyc_cfg):
        await _configure(session)
        row = await _active_session(session, client)
        session.add(_joined(row, biometric=True))
        await session.commit()
        with patch("case_service.meet.kyc.face_available", return_value=True):
            check = await meet_kyc._face_match_check(session, row, [])
        assert check["skipped"] is True and "no video frames" in check["detail"]

    async def test_no_verified_document_skips(self, client, session, kyc_cfg):
        await _configure(session)
        row = await _active_session(session, client)
        session.add(_joined(row, biometric=True))
        await session.commit()
        with patch("case_service.meet.kyc.face_available", return_value=True):
            check = await meet_kyc._face_match_check(session, row, [object()])
        assert check["skipped"] is True
        assert "no passed document verification" in check["detail"]


class TestFaceMatchScored:
    async def _scored_setup(self, client, session, row):
        """Verified ID doc on the case + fully-consented joined participant."""
        from case_service.documents.service import DocumentService
        doc = await DocumentService().upload(
            session, case_id=row.case_id, filename="id.png", data=b"fake-png",
            content_type="image/png", uploaded_by="test", tenant_id=row.tenant_id)
        session.add(DocumentVerificationModel(
            case_id=row.case_id, document_id=doc.id, status="passed",
            checks=[], verified_by="user:test-admin"))
        session.add(_joined(row, biometric=True))
        await session.commit()

    async def test_same_face_scores_low_risk(self, client, session, kyc_cfg):
        import numpy as np
        await _configure(session)
        row = await _active_session(session, client)
        await self._scored_setup(client, session, row)
        vec = np.ones(512, dtype=np.float32)
        with patch("case_service.meet.kyc.face_available", return_value=True), \
             patch("case_service.meet.kyc._embed_image_bytes", return_value=vec), \
             patch("case_service.meet.kyc._embed_frames", return_value=[vec]):
            check = await meet_kyc._face_match_check(session, row, [object()])
        assert not check.get("skipped")
        assert check["score"] == 0.0                     # identical embeddings
        assert "discarded" in check["detail"]

    async def test_different_face_scores_high_risk(self, client, session, kyc_cfg):
        import numpy as np
        await _configure(session)
        row = await _active_session(session, client)
        await self._scored_setup(client, session, row)
        a = np.zeros(512, dtype=np.float32); a[0] = 1.0
        b = np.zeros(512, dtype=np.float32); b[1] = 1.0   # orthogonal = strangers
        with patch("case_service.meet.kyc.face_available", return_value=True), \
             patch("case_service.meet.kyc._embed_image_bytes", return_value=a), \
             patch("case_service.meet.kyc._embed_frames", return_value=[b]):
            check = await meet_kyc._face_match_check(session, row, [object()])
        assert check["score"] == 1.0

    async def test_job_stores_scores_never_embeddings(self, client, session, kyc_cfg):
        """The compare-and-discard invariant, checked at the storage layer."""
        import numpy as np
        from case_service.documents.service import DocumentService
        await _configure(session)
        row = await _active_session(session, client)
        await self._scored_setup(client, session, row)
        # Make the session analyzable: sealed placeholder recording.
        doc = await DocumentService().upload(
            session, case_id=row.case_id, filename="rec.mp4.hxsealed",
            data=b"sealed", content_type="application/octet-stream",
            uploaded_by="hxmeet", tenant_id=row.tenant_id)
        row.status = "ended"
        row.recording_status = "sealed"
        row.recording_document_id = doc.id
        session.add(CaseSessionKycSignalsModel(session_id=row.id, requested_by="user:test-admin"))
        await session.commit()

        vec = np.ones(512, dtype=np.float32)
        frames = [np.full((32, 32), 128, dtype=np.uint8)] * 3
        with patch("case_service.meet.intelligence._unseal_recording",
                   new=AsyncMock(return_value=b"fake-mp4")), \
             patch("case_service.meet.kyc._decode_frames", return_value=frames), \
             patch("case_service.meet.kyc.face_available", return_value=True), \
             patch("case_service.meet.kyc._embed_image_bytes", return_value=vec), \
             patch("case_service.meet.kyc._embed_frames", return_value=[vec]), \
             patch("case_service.meet.kyc._unseal_transcript_text",
                   new=AsyncMock(return_value=None)):
            await meet_kyc.run_kyc_job(row.id)

        body = (await client.get(f"{MEET}/sessions/{row.id}/kyc-analysis")).json()
        assert body["status"] == "completed", body.get("error") or body
        face = next(c for c in body["checks"] if c["name"] == "face_match")
        assert face["score"] == 0.0
        # Nothing embedding-shaped may rest anywhere in the stored analysis.
        stored = json.dumps(body)
        assert "embedding" not in stored.replace("embeddings compared in memory and discarded", "")
        gan = next(c for c in body["checks"] if c["name"] == "gan_artifact")
        assert gan["skipped"] is True                    # named honest placeholder
