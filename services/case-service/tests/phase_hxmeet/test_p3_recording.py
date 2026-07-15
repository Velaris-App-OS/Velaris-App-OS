"""HxMeet P3 — sealed recording.

Pins: record_intent declared at start (embedded only, 501 without the egress
dir), join-time consent stamps (worker token mint + guest exchange), the
all-participant consent gate on recording start, the egress lifecycle
(start > stop > egress_ended webhook > ingest), the seal itself (plaintext
sha256 in the audit chain, tenant-DEK ciphertext as the case document),
meet.recording.view download/unseal + tamper detection, hash verification,
and the non-consuming public guest preview (notice before consent).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from unittest.mock import AsyncMock, patch

import jwt
import pytest
import pytest_asyncio

from case_service.config import get_settings
from case_service.db.models import CaseSessionParticipantModel
from sqlalchemy import select
from tests.conftest import create_case, deploy_case_type

MEET = "/api/v1/meet"
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42-fake-recording-bytes-" * 64


@pytest_asyncio.fixture
async def recording_cfg(tmp_path):
    """Embedded driver + LiveKit + a real temp recordings dir."""
    s = get_settings()
    s.meet_driver = "embedded"
    s.livekit_url = "wss://livekit.test"
    s.livekit_api_key = "lk_test_key"
    s.livekit_api_secret = "lk_test_secret_of_sufficient_length"
    s.meet_recordings_dir = str(tmp_path)
    yield s


def _patch_egress_start(egress_id="EG_test123"):
    return patch("case_service.meet.livekit.start_room_recording",
                 new=AsyncMock(return_value=egress_id))


def _patch_egress_stop():
    return patch("case_service.meet.livekit.stop_room_recording", new=AsyncMock())


def _sign_webhook(body: bytes) -> str:
    s = get_settings()
    return jwt.encode(
        {"iss": s.livekit_api_key,
         "sha256": base64.b64encode(hashlib.sha256(body).digest()).decode()},
        s.livekit_api_secret, algorithm="HS256",
    )


async def _post_webhook(client, event: dict):
    body = json.dumps(event).encode()
    return await client.post(
        f"{MEET}/webhook/livekit", content=body,
        headers={"Authorization": _sign_webhook(body), "Content-Type": "application/json"},
    )


async def _recorded_session(client) -> dict:
    ct = await deploy_case_type(client, name=f"Rec CT {uuid.uuid4().hex[:6]}")
    case = await create_case(client, ct["id"])
    r = await client.post(f"{MEET}/cases/{case['id']}/sessions",
                          json={"title": "KYC call", "record": True})
    assert r.status_code == 201, r.text
    return r.json()


async def _egress_ended(client, session: dict, egress_id: str, fname: str, status="EGRESS_COMPLETE"):
    return await _post_webhook(client, {
        "event": "egress_ended",
        "egressInfo": {"egressId": egress_id, "roomName": session["external_meeting_id"],
                       "status": status, "fileResults": [{"filename": f"/out/{fname}"}]},
    })


async def _full_recording(client, recording_cfg) -> dict:
    """start (record) > worker consents via token > start rec > egress_ended.
    The best-effort TSA anchor is patched out — no network in unit tests."""
    sess = await _recorded_session(client)
    assert (await client.post(f"{MEET}/sessions/{sess['id']}/token")).status_code == 200
    with _patch_egress_start():
        r = await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")
    assert r.status_code == 200, r.text
    fname = f"{sess['external_meeting_id']}-test.mp4"
    (get_settings().meet_recordings_dir and
     open(f"{get_settings().meet_recordings_dir}/{fname}", "wb").write(FAKE_MP4))
    with patch("case_service.compliance.audit_anchor.anchor_chain_tip",
               new=AsyncMock(return_value={"anchored": False, "reason": "test"})):
        assert (await _egress_ended(client, sess, "EG_test123", fname)).status_code == 200
    r = await client.get(f"{MEET}/cases/{sess['case_id']}/sessions")
    return next(s for s in r.json()["sessions"] if s["id"] == sess["id"])


class TestRecordIntent:
    async def test_record_start_and_status(self, client, recording_cfg):
        sess = await _recorded_session(client)
        assert sess["record_intent"] is True
        assert sess["recording_status"] == "none"

    async def test_record_without_egress_dir_501(self, client, recording_cfg):
        recording_cfg.meet_recordings_dir = ""
        ct = await deploy_case_type(client, name=f"Rec CT {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])
        r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={"record": True})
        assert r.status_code == 501

    async def test_record_on_off_platform_400(self, client, recording_cfg):
        recording_cfg.meet_driver = "off_platform"
        ct = await deploy_case_type(client, name=f"Rec CT {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])
        r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={"record": True})
        assert r.status_code == 400

    async def test_recording_start_needs_intent_409(self, client, recording_cfg):
        ct = await deploy_case_type(client, name=f"Rec CT {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])
        sess = (await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})).json()
        r = await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")
        assert r.status_code == 409


class TestConsent:
    async def test_worker_token_stamps_consent(self, client, session, recording_cfg):
        sess = await _recorded_session(client)
        await client.post(f"{MEET}/sessions/{sess['id']}/token")
        row = (await session.execute(
            select(CaseSessionParticipantModel)
            .where(CaseSessionParticipantModel.session_id == uuid.UUID(sess["id"]))
        )).scalars().one()
        assert row.consent_recorded_at is not None

    async def test_no_consent_on_unrecorded_session(self, client, session, recording_cfg):
        ct = await deploy_case_type(client, name=f"Rec CT {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])
        sess = (await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})).json()
        await client.post(f"{MEET}/sessions/{sess['id']}/token")
        row = (await session.execute(
            select(CaseSessionParticipantModel)
            .where(CaseSessionParticipantModel.session_id == uuid.UUID(sess["id"]))
        )).scalars().one()
        assert row.consent_recorded_at is None

    async def test_guest_exchange_stamps_consent(self, client, anon_client, session, recording_cfg):
        sess = await _recorded_session(client)
        inv = (await client.post(f"{MEET}/sessions/{sess['id']}/invites",
                                 json={"email": "g@example.com"})).json()
        r = await anon_client.post(f"{MEET}/guest/token", json={"invite_token": inv["invite_token"]})
        assert r.status_code == 200
        assert r.json()["record_intent"] is True
        row = (await session.execute(
            select(CaseSessionParticipantModel)
            .where(CaseSessionParticipantModel.session_id == uuid.UUID(sess["id"]),
                   CaseSessionParticipantModel.identity == "email:g@example.com")
        )).scalars().one()
        assert row.consent_recorded_at is not None

    async def test_joined_participant_without_consent_blocks_recording(
        self, client, session, recording_cfg,
    ):
        """A participant IN the room (webhook-joined) without a consent stamp
        hard-blocks recording start with 409 — the legal gate."""
        sess = await _recorded_session(client)
        await client.post(f"{MEET}/sessions/{sess['id']}/token")  # worker consents
        session.add(CaseSessionParticipantModel(
            session_id=uuid.UUID(sess["id"]), tenant_id="default",
            identity="email:lurker@example.com", role="guest",
        ))
        await session.commit()
        await _post_webhook(client, {"event": "participant_joined",
                                     "room": {"name": sess["external_meeting_id"]},
                                     "participant": {"identity": "email:lurker@example.com"}})
        with _patch_egress_start():
            r = await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")
        assert r.status_code == 409
        assert "lurker@example.com" in r.json()["detail"]

    async def test_guest_preview_is_nonconsuming_and_shows_intent(
        self, client, anon_client, recording_cfg,
    ):
        sess = await _recorded_session(client)
        inv = (await client.post(f"{MEET}/sessions/{sess['id']}/invites",
                                 json={"email": "g@example.com"})).json()
        p = await anon_client.post(f"{MEET}/guest/preview",
                                   json={"invite_token": inv["invite_token"]})
        assert p.status_code == 200
        assert p.json()["record_intent"] is True
        # Preview did NOT consume — the exchange still works.
        r = await anon_client.post(f"{MEET}/guest/token",
                                   json={"invite_token": inv["invite_token"]})
        assert r.status_code == 200

    async def test_guest_preview_bad_token_404(self, anon_client, recording_cfg):
        r = await anon_client.post(f"{MEET}/guest/preview", json={"invite_token": "nope"})
        assert r.status_code == 404


class TestRecordingLifecycle:
    async def test_start_stop_roundtrip(self, client, recording_cfg):
        sess = await _recorded_session(client)
        await client.post(f"{MEET}/sessions/{sess['id']}/token")
        with _patch_egress_start():
            r = await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")
        assert r.status_code == 200
        assert r.json()["recording_status"] == "recording"
        with _patch_egress_stop():
            r2 = await client.post(f"{MEET}/sessions/{sess['id']}/recording/stop")
        assert r2.status_code == 200
        assert r2.json()["recording_status"] == "processing"

    async def test_double_start_409(self, client, recording_cfg):
        sess = await _recorded_session(client)
        await client.post(f"{MEET}/sessions/{sess['id']}/token")
        with _patch_egress_start():
            await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")
            r = await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")
        assert r.status_code == 409

    async def test_egress_failure_marks_failed(self, client, recording_cfg):
        sess = await _recorded_session(client)
        await client.post(f"{MEET}/sessions/{sess['id']}/token")
        with _patch_egress_start():
            await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")
        await _egress_ended(client, sess, "EG_test123", "missing.mp4", status="EGRESS_FAILED")
        r = await client.get(f"{MEET}/cases/{sess['case_id']}/sessions")
        assert r.json()["sessions"][0]["recording_status"] == "failed"


class TestSeal:
    async def test_egress_complete_seals_recording(self, client, recording_cfg):
        final = await _full_recording(client, recording_cfg)
        assert final["recording_status"] == "sealed"
        assert final["recording_document_id"]

    async def test_stored_document_is_ciphertext(self, client, session, recording_cfg):
        final = await _full_recording(client, recording_cfg)
        from case_service.documents.service import DocumentService
        data, name, _ct = await DocumentService().download(
            session, uuid.UUID(final["recording_document_id"]))
        assert name.endswith(".hxsealed")
        assert FAKE_MP4[:32] not in data           # DEK-sealed, not plaintext

    async def test_download_unseals_plaintext(self, client, recording_cfg):
        final = await _full_recording(client, recording_cfg)
        r = await client.get(f"{MEET}/sessions/{final['id']}/recording")
        assert r.status_code == 200
        assert r.content == FAKE_MP4
        assert r.headers["content-type"].startswith("video/mp4")

    async def test_verify_confirms_hash(self, client, recording_cfg):
        final = await _full_recording(client, recording_cfg)
        r = await client.get(f"{MEET}/sessions/{final['id']}/recording/verify")
        assert r.status_code == 200
        body = r.json()
        assert body["verified"] is True
        assert body["sha256"] == hashlib.sha256(FAKE_MP4).hexdigest()
        assert "chain-seq:" in body["anchor_ref"]

    async def test_tamper_detected(self, client, session, recording_cfg):
        """Flip a ciphertext byte in storage — AES-GCM must refuse to unseal."""
        final = await _full_recording(client, recording_cfg)
        from case_service.documents.service import DocumentService
        svc = DocumentService()
        doc_id = uuid.UUID(final["recording_document_id"])
        data, name, _ct = await svc.download(session, doc_id)
        tampered = bytes([data[0] ^ 0xFF]) + data[1:]
        await svc.overwrite(session, doc_id, tampered)
        await session.commit()
        r = await client.get(f"{MEET}/sessions/{final['id']}/recording")
        assert r.status_code == 409
        v = await client.get(f"{MEET}/sessions/{final['id']}/recording/verify")
        assert v.json()["verified"] is False

    async def test_temp_file_deleted_after_ingest(self, client, recording_cfg):
        import os
        await _full_recording(client, recording_cfg)
        assert not [f for f in os.listdir(recording_cfg.meet_recordings_dir)
                    if f.endswith(".mp4")]

    async def test_seal_lands_in_audit_chain(self, client, session, recording_cfg):
        final = await _full_recording(client, recording_cfg)
        from case_service.compliance.audit_chain import verify_chain
        assert (await verify_chain(session))["verified"] is True
        from case_service.db.models import CaseAuditLogModel
        row = (await session.execute(
            select(CaseAuditLogModel)
            .where(CaseAuditLogModel.action == "meet.recording.sealed")
        )).scalars().first()
        assert row is not None
        assert row.details["sha256"] == hashlib.sha256(FAKE_MP4).hexdigest()


class TestRecordingAccess:
    async def test_recording_view_registered_with_hxguard(self, client, recording_cfg):
        """Enforce mode: meet.recording.view must be registered; an unrelated
        user gets the 404 anti-oracle."""
        from case_service.auth.jwt_handler import create_dev_token
        final = await _full_recording(client, recording_cfg)
        s = get_settings()
        s.hxguard_case_enforcement = "enforce"
        assert (await client.get(f"{MEET}/sessions/{final['id']}/recording")).status_code == 200

        outsider = create_dev_token(
            user_id=str(uuid.uuid4()), username="outsider", roles=["viewer"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
        )
        r = await client.get(f"{MEET}/sessions/{final['id']}/recording",
                             headers={"Authorization": f"Bearer {outsider}"})
        assert r.status_code == 404

    async def test_no_recording_404(self, client, recording_cfg):
        sess = await _recorded_session(client)
        r = await client.get(f"{MEET}/sessions/{sess['id']}/recording")
        assert r.status_code == 404
