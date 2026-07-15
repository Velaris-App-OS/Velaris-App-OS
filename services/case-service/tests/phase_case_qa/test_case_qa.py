"""HxNexus case-scoped Q&A — sovereignty-first ask-the-case.

Pins: the tenant opt-in gate (400 when off), the per-tenant egress choice
(local_only DEFAULT forces the local backend even when an external provider
is configured platform-wide; external_allowed = tenant consent, external
answers directly and the egress is audited), permission-gated transcript
feeding (withheld + disclosed without meet.recording.view), the case_qa_asked
audit row naming sources, and the cases.ask HxGuard registration (the
enforce-mode fail-closed gotcha). LLM always mocked.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from case_service.db.models import CaseSessionModel, TenantModel
from case_service.hxnexus import case_qa
from sqlalchemy import select
from tests.conftest import create_case, deploy_case_type

pytestmark = pytest.mark.asyncio

NEXUS = "/api/v1/hxnexus"


class FakeLocal:
    available = True
    is_external = False
    backend_name = "ollama"
    _model = "llama3.2"

    def __init__(self, answer="The customer requested a refund [S1]."):
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answer


class FakeExternal(FakeLocal):
    is_external = True
    backend_name = "openai"
    prefer_external = False
    last_route = "external"
    suppress_generic_audit = False


async def _tenant_ai(session, cfg: dict):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == "default"))).scalars().first()
    if tenant is None:
        tenant = TenantModel(slug="default", name="Default")
        session.add(tenant)
    tenant.settings = {**(tenant.settings or {}), "ai": cfg}
    session.add(tenant)
    await session.commit()


async def _case(session, client):
    ct = await deploy_case_type(client, name=f'QA CT {uuid.uuid4().hex[:6]}')
    case = await create_case(client, ct['id'])
    from case_service.db.models import CaseInstanceModel
    return await session.get(CaseInstanceModel, uuid.UUID(case['id']))


class TestGates:
    async def test_disabled_tenant_400(self, client, session):
        await _tenant_ai(session, {})
        row = await _case(session, client)
        r = await client.post(f"{NEXUS}/cases/{row.id}/ask", json={"question": "What happened?"})
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"]

    async def test_unauthenticated_401(self, anon_client, client, session):
        row = await _case(session, client)
        r = await anon_client.post(f"{NEXUS}/cases/{row.id}/ask", json={"question": "hi"})
        assert r.status_code == 401

    async def test_cases_ask_registered_in_hxguard(self):
        from case_service.hxguard.service import CASE_ACTIONS
        assert CASE_ACTIONS["cases.ask"] == {"assignee", "editor", "viewer"}

    async def test_backend_unavailable_503(self, client, session):
        await _tenant_ai(session, {"case_qa": True})
        row = await _case(session, client)
        dead = FakeLocal(); dead.available = False
        with patch.object(case_qa, "_local_backend", return_value=dead):
            r = await client.post(f"{NEXUS}/cases/{row.id}/ask", json={"question": "hi"})
        assert r.status_code == 503


class TestSovereignty:
    async def test_local_only_forces_local_even_with_external_configured(self, client, session):
        """The default. get_llm_backend (the platform provider, possibly
        external) must NOT even be consulted."""
        await _tenant_ai(session, {"case_qa": True})   # egress omitted = local_only
        row = await _case(session, client)
        local = FakeLocal()

        def _boom():
            raise AssertionError("local_only must never resolve the platform provider")

        with patch.object(case_qa, "_local_backend", return_value=local), \
             patch.object(case_qa, "get_llm_backend", _boom):
            r = await client.post(f"{NEXUS}/cases/{row.id}/ask",
                                  json={"question": "What is this case about?"})
        assert r.status_code == 200
        body = r.json()
        assert body["external_ai"] is False
        assert "refund" in body["answer"]
        assert local.prompts, "local backend answered"
        assert any(s["kind"] == "case" for s in body["sources"])

    async def test_tenant_model_override_applies_locally(self, client, session):
        await _tenant_ai(session, {"case_qa": True, "model": "qwen2.5"})
        row = await _case(session, client)
        local = FakeLocal()
        with patch.object(case_qa, "_local_backend", return_value=local):
            r = await client.post(f"{NEXUS}/cases/{row.id}/ask", json={"question": "q"})
        assert r.status_code == 200
        assert local._model == "qwen2.5"

    async def test_external_allowed_consents_and_audits(self, client, session):
        await _tenant_ai(session, {"case_qa": True, "egress": "external_allowed"})
        row = await _case(session, client)
        ext = FakeExternal()
        egress = AsyncMock()
        with patch.object(case_qa, "get_llm_backend", return_value=ext), \
             patch("case_service.hxnexus.egress_audit.record_egress", egress):
            r = await client.post(f"{NEXUS}/cases/{row.id}/ask",
                                  json={"question": "Summarize the case"})
        assert r.status_code == 200
        body = r.json()
        assert body["external_ai"] is True
        assert ext.prefer_external is True          # consent = external answers directly
        egress.assert_awaited_once()                # every egress lands in the audit
        assert egress.await_args.kwargs["purpose"] == "case_qa"
        assert egress.await_args.kwargs["pseudonymized"] is True

    async def test_external_context_is_pseudonymized(self, client, session):
        """What actually leaves must be the redacted text, not the raw case."""
        await _tenant_ai(session, {"case_qa": True, "egress": "external_allowed"})
        row = await _case(session, client)
        ext = FakeExternal()
        with patch.object(case_qa, "get_llm_backend", return_value=ext), \
             patch("case_service.hxnexus.egress_audit.record_egress", AsyncMock()):
            r = await client.post(
                f"{NEXUS}/cases/{row.id}/ask",
                json={"question": "Email john.doe@example.com about the case"})
        assert r.status_code == 200
        sent = ext.prompts[0]
        assert "john.doe@example.com" not in sent   # pseudonymized before egress


class TestTranscriptGate:
    async def _sealed_session(self, session, row):
        s = CaseSessionModel(
            case_id=row.id, tenant_id="default", driver="embedded", provider="livekit",
            status="ended", title="KYC call", started_by="user:t", record_intent=True,
            transcript_status="sealed", transcript_document_id=uuid.uuid4(),
            external_meeting_id=f"vx-default-{uuid.uuid4()}",
        )
        session.add(s)
        await session.commit()
        return s

    async def test_transcript_withheld_without_permission(self, client, session):
        await _tenant_ai(session, {"case_qa": True})
        row = await _case(session, client)
        await self._sealed_session(session, row)
        local = FakeLocal()
        with patch.object(case_qa, "_local_backend", return_value=local):
            result = await case_qa.ask_case(
                session, case=row, question="what was said?",
                user=type("U", (), {"user_id": "u1"})(),
                can_view_transcripts=False, backend=local)
        assert result["withheld"] and "transcript" in result["withheld"][0]
        assert not any(s["kind"] == "transcript" for s in result["sources"])
        assert "withheld" in local.prompts[0]       # the model is told, honestly

    async def test_transcript_fed_with_permission(self, client, session):
        await _tenant_ai(session, {"case_qa": True})
        row = await _case(session, client)
        await self._sealed_session(session, row)
        local = FakeLocal()
        with patch("case_service.api.routers.meet._unseal_transcript",
                   new=AsyncMock(return_value=b"[10:00:00] Guest: I confirm my DOB.")):
            result = await case_qa.ask_case(
                session, case=row, question="what was said?",
                user=type("U", (), {"user_id": "u1"})(),
                can_view_transcripts=True, backend=local)
        assert any(s["kind"] == "transcript" for s in result["sources"])
        assert result["withheld"] == []
        assert "I confirm my DOB" in local.prompts[0]


class TestAudit:
    async def test_every_ask_writes_case_audit(self, client, session):
        await _tenant_ai(session, {"case_qa": True})
        row = await _case(session, client)
        with patch.object(case_qa, "_local_backend", return_value=FakeLocal()):
            r = await client.post(f"{NEXUS}/cases/{row.id}/ask",
                                  json={"question": "Anything open on this case?"})
        assert r.status_code == 200
        from case_service.db.models import CaseAuditLogModel
        session.expire_all()
        rows = (await session.execute(
            select(CaseAuditLogModel).where(CaseAuditLogModel.action == "case_qa_asked")
        )).scalars().all()
        mine = [a for a in rows if a.details.get("question", "").startswith("Anything open")]
        assert mine
        assert mine[0].details["external_ai"] is False
        assert any(s["kind"] == "case" for s in mine[0].details["sources"])
