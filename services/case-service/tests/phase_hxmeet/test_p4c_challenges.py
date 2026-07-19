"""HxMeet P4c — Video-KYC liveness challenges.

Pins: tenant opt-in gate (400 when off), record-intent requirement (409),
recording-must-be-running requirement (409 — the challenge and the response
must land in the sealed recording), server-side randomized payloads (CSPRNG,
correct shape per kind), unknown-kind 400, worker result recording (human
judgment: passed/failed/skipped + notes, stamped), cross-session challenge =
uniform 404, meet.kyc.run enforce-mode registration + anti-oracle, and the
audit events.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import select

from case_service.config import get_settings
from case_service.db.models import CaseSessionModel, TenantModel
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


async def _enable_kyc(session, slug="default"):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug))).scalars().first()
    if tenant is None:
        tenant = TenantModel(slug=slug, name=slug, settings={})
        session.add(tenant)
    settings = dict(tenant.settings or {})
    settings["meet"] = {**settings.get("meet", {}), "kyc": True}
    tenant.settings = settings
    await session.commit()


async def _session_row(
    session, case_id, tenant_slug="default", *,
    status="active", record_intent=True, recording_status="recording",
) -> CaseSessionModel:
    row = CaseSessionModel(
        case_id=case_id, tenant_id=tenant_slug, driver="embedded",
        provider="livekit", status=status, title="KYC call",
        started_by="user:test-admin", record_intent=record_intent,
        recording_status=recording_status,
        external_meeting_id=f"vx-{tenant_slug}-{uuid.uuid4()}",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _kyc_session(client, session, **kw) -> CaseSessionModel:
    ct = await deploy_case_type(client, name=f'P4c CT {uuid.uuid4().hex[:6]}')
    case = await create_case(client, ct['id'])
    return await _session_row(session, uuid.UUID(case['id']), **kw)


class TestChallengeGates:
    async def test_tenant_gate_off_400(self, client, session, kyc_cfg):
        row = await _kyc_session(client, session)
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"]

    async def test_requires_record_intent_409(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session,
                                 record_intent=False, recording_status="none")
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        assert r.status_code == 409
        assert "record-intent" in r.json()["detail"]

    async def test_requires_recording_running_409(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session, recording_status="none")
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        assert r.status_code == 409
        assert "recording is running" in r.json()["detail"]

    async def test_inactive_session_409(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session, status="ended")
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        assert r.status_code == 409

    async def test_unknown_kind_400(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session)
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges",
                              json={"kinds": ["head_turn", "blood_sample"]})
        assert r.status_code == 400
        assert "blood_sample" in r.json()["detail"]

    async def test_unknown_session_404(self, client, kyc_cfg):
        r = await client.post(f"{MEET}/sessions/{uuid.uuid4()}/challenges", json={})
        assert r.status_code == 404


class TestChallengeScript:
    async def test_default_script_mints_all_kinds(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session)
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        assert r.status_code == 201, r.text
        challenges = r.json()["challenges"]
        assert {c["kind"] for c in challenges} == {"head_turn", "phrase_readback", "document_tilt"}
        for c in challenges:
            assert c["result"] == "pending"
            assert c["issued_by"]          # requesting worker's user id
            assert c["payload"]["instruction"]

    async def test_payload_shapes_are_randomized_server_side(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session)
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        by_kind = {c["kind"]: c["payload"] for c in r.json()["challenges"]}
        digits = by_kind["phrase_readback"]["phrase"].split()
        assert len(digits) == 6 and all(d.isdigit() for d in digits)
        assert len(by_kind["head_turn"]["sequence"]) == 3
        assert set(by_kind["head_turn"]["sequence"]) <= {"left", "right", "up"}
        assert by_kind["document_tilt"]["side"] in ("left", "right")

    async def test_subset_kinds(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session)
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges",
                              json={"kinds": ["phrase_readback"]})
        challenges = r.json()["challenges"]
        assert len(challenges) == 1 and challenges[0]["kind"] == "phrase_readback"

    async def test_list_challenges(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session)
        await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        await client.post(f"{MEET}/sessions/{row.id}/challenges",
                          json={"kinds": ["phrase_readback"]})
        r = await client.get(f"{MEET}/sessions/{row.id}/challenges")
        assert r.status_code == 200
        assert len(r.json()["challenges"]) == 4

    async def test_issue_writes_audit_event(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session)
        await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        from case_service.db.models import CaseAuditLogModel
        audit = (await session.execute(
            select(CaseAuditLogModel)
            .where(CaseAuditLogModel.action == "kyc_challenges_issued")
        )).scalars().first()
        assert audit is not None
        assert audit.details["session_id"] == str(row.id)


class TestChallengeResults:
    async def _issued(self, client, session, kyc_cfg):
        await _enable_kyc(session)
        row = await _kyc_session(client, session)
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        return row, r.json()["challenges"]

    async def test_worker_records_result(self, client, session, kyc_cfg):
        row, challenges = await self._issued(client, session, kyc_cfg)
        cid = challenges[0]["id"]
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges/{cid}/result",
                              json={"result": "passed", "notes": "clean head turn"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["result"] == "passed"
        assert body["result_notes"] == "clean head turn"
        assert body["result_by"]           # recording worker's user id
        assert body["result_at"]

    async def test_invalid_result_400(self, client, session, kyc_cfg):
        row, challenges = await self._issued(client, session, kyc_cfg)
        r = await client.post(
            f"{MEET}/sessions/{row.id}/challenges/{challenges[0]['id']}/result",
            json={"result": "verified"})
        assert r.status_code == 400

    async def test_cross_session_challenge_404(self, client, session, kyc_cfg):
        row, challenges = await self._issued(client, session, kyc_cfg)
        other = await _kyc_session(client, session)
        r = await client.post(
            f"{MEET}/sessions/{other.id}/challenges/{challenges[0]['id']}/result",
            json={"result": "passed"})
        assert r.status_code == 404

    async def test_unknown_challenge_404(self, client, session, kyc_cfg):
        row, _ = await self._issued(client, session, kyc_cfg)
        r = await client.post(
            f"{MEET}/sessions/{row.id}/challenges/{uuid.uuid4()}/result",
            json={"result": "passed"})
        assert r.status_code == 404

    async def test_result_writes_audit_event(self, client, session, kyc_cfg):
        row, challenges = await self._issued(client, session, kyc_cfg)
        await client.post(
            f"{MEET}/sessions/{row.id}/challenges/{challenges[0]['id']}/result",
            json={"result": "failed"})
        from case_service.db.models import CaseAuditLogModel
        audit = (await session.execute(
            select(CaseAuditLogModel)
            .where(CaseAuditLogModel.action == "kyc_challenge_result")
        )).scalars().first()
        assert audit is not None
        assert audit.details["result"] == "failed"


class TestKycAuthorization:
    async def test_kyc_run_registered_with_hxguard(self, client, session, kyc_cfg):
        """Enforce mode: meet.kyc.run must be registered; an unrelated user
        gets the 404 anti-oracle, the case owner still passes."""
        from case_service.auth.jwt_handler import create_dev_token
        await _enable_kyc(session)
        row = await _kyc_session(client, session)
        s = get_settings()
        s.hxguard_case_enforcement = "enforce"
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={})
        assert r.status_code == 201, r.text

        outsider = create_dev_token(
            user_id=str(uuid.uuid4()), username="outsider", roles=["viewer"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
        )
        r = await client.post(f"{MEET}/sessions/{row.id}/challenges", json={},
                              headers={"Authorization": f"Bearer {outsider}"})
        assert r.status_code == 404
