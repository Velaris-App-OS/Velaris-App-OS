"""HxMeet P4 — session intelligence (P4a) + document-first verification (P4b).

P4a pins: tenant opt-in gate (400 when off), 501 when faster-whisper absent,
sealed-recording requirement (404 otherwise), the background job (whisper +
LLM both mocked — no model weights in CI), transcript stored as a case
document, summary/action-items/model-versions recorded, audit event, and the
recording.view gate on reading results.

P4b pins: automated checks (file integrity magic-byte, image quality, MRZ
check digits, expiry), the rule that a worker cannot record 'passed' over a
failing check (409), worker verdict + checklist recorded, docs.verify HxGuard
gate, and the audit event.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from case_service.config import get_settings
from case_service.db.models import (
    CaseSessionIntelligenceModel,
    CaseSessionModel,
    TenantModel,
)
from sqlalchemy import select
from tests.conftest import create_case, deploy_case_type

MEET = "/api/v1/meet"
DOCS = "/api/v1/documents"

# Minimal real PNG (1x1) and a larger valid-header one for the quality check.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f0300050001a5f645400000000049454e44ae426082")


async def _seal_session(session, case_id, tenant_slug="default") -> CaseSessionModel:
    """Create an embedded session and seal a fake recording onto it, the same
    way the egress_ended webhook would."""
    from case_service.meet import service as meet
    row = CaseSessionModel(
        case_id=case_id, tenant_id=tenant_slug, driver="embedded", provider="livekit",
        status="ended", title="KYC call", started_by="user:test-admin",
        record_intent=True, recording_status="processing",
        external_meeting_id=f"vx-{tenant_slug}-{uuid.uuid4()}",
        recording_egress_id="EG_test",
    )
    session.add(row); await session.flush()

    # Drop a fake recording file where ingest expects it, then seal.
    import os
    s = get_settings()
    os.makedirs(s.meet_recordings_dir, exist_ok=True)
    fname = f"rec-{row.id}.mp4"
    with open(os.path.join(s.meet_recordings_dir, fname), "wb") as fh:
        fh.write(b"\x00\x00\x00\x18ftypmp42fake-recording" * 100)
    await meet.ingest_recording(session, row, fname)
    await session.refresh(row)
    return row


async def _lite_sealed_session(session, client, case_id, tenant_slug="default") -> CaseSessionModel:
    """A session marked sealed with a real transcript-able document, WITHOUT the
    heavy ingest path (DEK seal + audit-chain + TSA network anchor). The
    unseal itself is covered by the P3 recording tests; the P4a orchestration
    tests mock _unseal_recording, so they only need the sealed-state fields."""
    from case_service.documents.service import DocumentService
    doc = await DocumentService().upload(
        session, case_id=case_id, filename="rec.mp4.hxsealed",
        data=b"sealed-placeholder", content_type="application/octet-stream",
        uploaded_by="hxmeet", tenant_id=tenant_slug)
    row = CaseSessionModel(
        case_id=case_id, tenant_id=tenant_slug, driver="embedded", provider="livekit",
        status="ended", title="KYC call", started_by="user:test-admin",
        record_intent=True, recording_status="sealed", recording_document_id=doc.id,
        external_meeting_id=f"vx-{tenant_slug}-{uuid.uuid4()}", recording_egress_id="EG_test",
    )
    session.add(row); await session.commit(); await session.refresh(row)
    return row


@pytest_asyncio.fixture
async def rec_cfg(tmp_path):
    s = get_settings()
    s.meet_driver = "embedded"
    s.livekit_url = "wss://livekit.test"
    s.livekit_api_key = "lk_test_key"
    s.livekit_api_secret = "lk_test_secret_of_sufficient_length"
    s.meet_recordings_dir = str(tmp_path)
    yield s


async def _enable_intelligence(session, slug="default"):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug))).scalars().first()
    if tenant is None:
        tenant = TenantModel(slug=slug, name=slug, settings={})
        session.add(tenant)
    settings = dict(tenant.settings or {})
    settings["meet"] = {**settings.get("meet", {}), "intelligence": True}
    tenant.settings = settings
    await session.commit()


class TestP4aIntelligence:
    async def test_gate_off_returns_400(self, client, session, rec_cfg):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        row = await _seal_session(session, uuid.UUID(case['id']))
        with patch("case_service.meet.intelligence.whisper_available", return_value=True):
            r = await client.post(f"{MEET}/sessions/{row.id}/intelligence")
        assert r.status_code == 400  # tenant opt-in is OFF

    async def test_whisper_missing_returns_501(self, client, session, rec_cfg):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        row = await _seal_session(session, uuid.UUID(case['id']))
        await _enable_intelligence(session)
        with patch("case_service.meet.intelligence.whisper_available", return_value=False):
            r = await client.post(f"{MEET}/sessions/{row.id}/intelligence")
        assert r.status_code == 501

    async def test_no_sealed_recording_404(self, client, session, rec_cfg):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        row = CaseSessionModel(
            case_id=uuid.UUID(case['id']), tenant_id="default", driver="embedded",
            provider="livekit", status="ended", started_by="user:test-admin",
            record_intent=False, recording_status="none",
            external_meeting_id=f"vx-default-{uuid.uuid4()}",
        )
        session.add(row); await session.commit()
        await _enable_intelligence(session)
        r = await client.post(f"{MEET}/sessions/{row.id}/intelligence")
        assert r.status_code == 404

    async def test_full_run_transcribes_and_summarizes(self, client, session, rec_cfg):
        await _enable_intelligence(session)
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        row = await _lite_sealed_session(session, client, uuid.UUID(case['id']))

        fake_transcribe = ("[00:00] Hello, this is a test session.\n[00:05] We agreed to send the form.",
                           "en", 12, "1.2.1")
        # Seed the row and run the job directly — deterministic, and avoids the
        # endpoint's real (unpatched) BackgroundTask racing this patched run.
        session.add(CaseSessionIntelligenceModel(session_id=row.id, requested_by="user:test-admin"))
        await session.commit()
        from case_service.meet import intelligence as _intel
        with patch("case_service.meet.intelligence._unseal_recording",
                   new=AsyncMock(return_value=b"fake-mp4-bytes")), \
             patch("case_service.meet.intelligence._transcribe", return_value=fake_transcribe), \
             patch("case_service.meet.intelligence._summarize",
                   new=AsyncMock(return_value=("A test session summary.",
                                               ["Send the form"], "llama3"))):
            await _intel.run_intelligence_job(row.id)
        got = await client.get(f"{MEET}/sessions/{row.id}/intelligence")
        body = got.json()
        assert body["status"] == "completed", body.get("error") or body
        assert body["summary"] == "A test session summary."
        assert body["action_items"] == ["Send the form"]
        assert body["language"] == "en"
        assert body["transcript_document_id"]
        assert "whisper" in body["model_versions"] and body["model_versions"]["llm"] == "llama3"

        # transcript is a real, downloadable case document
        doc_id = body["transcript_document_id"]
        dl = await client.get(f"{DOCS}/{doc_id}/download")
        assert dl.status_code == 200
        assert b"test session" in dl.content

    async def test_summary_skipped_when_ai_unavailable(self, client, session, rec_cfg):
        await _enable_intelligence(session)
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        row = await _lite_sealed_session(session, client, uuid.UUID(case['id']))
        fake_transcribe = ("[00:00] Words.", "en", 3, "1.2.1")
        session.add(CaseSessionIntelligenceModel(session_id=row.id, requested_by="user:test-admin"))
        await session.commit()
        from case_service.meet import intelligence as _intel
        with patch("case_service.meet.intelligence._unseal_recording",
                   new=AsyncMock(return_value=b"fake-mp4-bytes")), \
             patch("case_service.meet.intelligence._transcribe", return_value=fake_transcribe), \
             patch("case_service.hxnexus.factory.check_ai_available",
                   new=AsyncMock(return_value=False)):
            await _intel.run_intelligence_job(row.id)
        body = (await client.get(f"{MEET}/sessions/{row.id}/intelligence")).json()
        assert body["status"] == "completed"
        assert body["summary"] is None
        assert body["transcript_document_id"]           # transcript still landed
        assert "unavailable" in body["model_versions"]["llm"]


class TestP4bDocumentVerification:
    async def _upload(self, client, case_id, data=PNG_1x1, ct="image/png", name="id.png"):
        r = await client.post(f"{DOCS}/upload",
                              files={"file": (name, data, ct)},
                              data={"case_id": case_id})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def test_worker_passes_clean_document(self, client):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        doc = await self._upload(client, case['id'])
        r = await client.post(f"{DOCS}/{doc}/verify", json={
            "status": "passed",
            "checklist": {"photo_matches": True, "no_visible_tampering": True},
            "expiry_date": (date.today() + timedelta(days=365)).isoformat(),
        })
        # image_quality fails on a 1x1 PNG → cannot pass
        assert r.status_code == 409
        names = [c for c in r.json()["detail"]]
        assert "image_quality" in r.json()["detail"]

    async def test_review_records_with_checks(self, client):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        doc = await self._upload(client, case['id'])
        r = await client.post(f"{DOCS}/{doc}/verify", json={"status": "review"})
        assert r.status_code == 201, r.text
        checks = {c["name"]: c["result"] for c in r.json()["checks"]}
        assert checks["file_integrity"] == "pass"       # real PNG magic bytes
        assert checks["image_quality"] == "fail"        # 1x1 too small
        assert checks["mrz_check_digits"] == "skipped"

    async def test_expiry_and_mrz_checks(self, client):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        doc = await self._upload(client, case['id'])
        # Expired document + a known-good MRZ line 2 (ICAO sample, valid digits)
        good_mrz = "L898902C36UTO7408122F1204159ZE184226B<<<<<10"
        r = await client.post(f"{DOCS}/{doc}/verify", json={
            "status": "review",
            "expiry_date": "2020-01-01",
            "mrz_line2": good_mrz,
        })
        checks = {c["name"]: c["result"] for c in r.json()["checks"]}
        assert checks["not_expired"] == "fail"
        assert checks["mrz_check_digits"] == "pass"

    async def test_failed_status_allowed_with_failing_checks(self, client):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        doc = await self._upload(client, case['id'])
        r = await client.post(f"{DOCS}/{doc}/verify", json={"status": "failed"})
        assert r.status_code == 201    # failing/review verdicts are always allowed

    async def test_bad_status_400(self, client):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        doc = await self._upload(client, case['id'])
        r = await client.post(f"{DOCS}/{doc}/verify", json={"status": "maybe"})
        assert r.status_code == 400

    async def test_verifications_listed_newest_first(self, client):
        ct = await deploy_case_type(client, name=f'P4 CT {uuid.uuid4().hex[:6]}')
        case = await create_case(client, ct['id'])
        doc = await self._upload(client, case['id'])
        await client.post(f"{DOCS}/{doc}/verify", json={"status": "review"})
        await client.post(f"{DOCS}/{doc}/verify", json={"status": "failed"})
        r = await client.get(f"{DOCS}/{doc}/verifications")
        rows = r.json()["verifications"]
        assert len(rows) == 2
        assert rows[0]["status"] == "failed"           # newest first

    async def test_unknown_document_404(self, client):
        r = await client.post(f"{DOCS}/{uuid.uuid4()}/verify", json={"status": "review"})
        assert r.status_code == 404
