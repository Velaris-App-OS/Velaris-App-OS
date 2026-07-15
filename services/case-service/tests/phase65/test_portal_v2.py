"""Portal v2 P1 — accounts-as-spine: sliding refresh, submit auto-link,
customer-JWT case endpoints (detail/timeline/documents/sessions), friendly
stage labels, and the portal-display admin config.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from case_service.db.models import (
    CaseInstanceModel,
    CaseSessionModel,
    CaseSessionParticipantModel,
    CaseTypeModel,
    PortalCustomerCaseLinkModel,
    PortalCustomerModel,
    TenantModel,
)

PORTAL = "/api/v1/portal"
PADMIN = "/api/v1/portal-admin"

STAGES = [
    {"id": "submission", "name": "Submission", "order": 0, "steps": []},
    {"id": "review",     "name": "Review",     "order": 1, "steps": []},
    {"id": "decision",   "name": "Decision",   "order": 2, "steps": []},
]


@pytest.fixture(autouse=True)
def _enable_customer_accounts():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("customer_accounts")
    _ENABLED_VERSIONS["customer_accounts"] = "v1.0.1"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("customer_accounts", None)
    else:
        _ENABLED_VERSIONS["customer_accounts"] = prior


@pytest_asyncio.fixture
async def tenant(session) -> TenantModel:
    t = TenantModel(slug="acme", name="ACME Corp",
                    settings={"portal": {"enabled": True}})
    session.add(t); await session.commit(); return t


@pytest_asyncio.fixture
async def case_type(session) -> CaseTypeModel:
    ct = CaseTypeModel(name="Support-v2", version="1.0",
                       definition_json={"stages": STAGES}, portal_enabled=True)
    session.add(ct); await session.commit(); return ct


def _capture_otp():
    sent: list[str] = []

    async def _fake(session, to_email, otp, purpose="login"):
        sent.append(otp)

    return sent, patch(
        "case_service.api.routers.portal_customers._send_otp_email",
        AsyncMock(side_effect=_fake),
    )


async def _login(client, slug: str, email: str) -> str:
    sent, patcher = _capture_otp()
    with patcher:
        r = await client.post(f"{PORTAL}/{slug}/auth/register",
                              json={"email": email, "display_name": "Jane Doe"})
        assert r.status_code == 200, r.text
    r2 = await client.post(f"{PORTAL}/{slug}/auth/verify-otp",
                           json={"email": email, "otp": sent[-1]})
    assert r2.status_code == 200, r2.text
    return r2.json()["customer_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _linked_case(session, tenant, case_type, email: str,
                       stage: str | None = "review", status: str = "open") -> CaseInstanceModel:
    case = CaseInstanceModel(
        case_type_id=case_type.id, case_type_version="1.0", status=status,
        priority="medium", tenant_id=tenant.id,
        portal_submitter_email=email, portal_tracking_token=uuid.uuid4(),
        current_stage_id=stage,
        data={"subject": "Rail case"}, created_by=f"portal:{email}",
    )
    session.add(case); await session.flush()
    customer = (await session.execute(
        __import__("sqlalchemy").select(PortalCustomerModel)
        .where(PortalCustomerModel.primary_email == email,
               PortalCustomerModel.tenant_id == tenant.id)
    )).scalar_one()
    session.add(PortalCustomerCaseLinkModel(customer_id=customer.id, case_id=case.id))
    await session.commit()
    return case


class TestSlidingRefresh:
    async def test_valid_token_refreshes(self, client, tenant):
        tok = await _login(client, "acme", "jane@example.com")
        r = await client.post(f"{PORTAL}/acme/auth/refresh", headers=_hdr(tok))
        assert r.status_code == 200
        assert r.json()["customer_token"]

    async def test_no_token_401(self, client, tenant):
        r = await client.post(f"{PORTAL}/acme/auth/refresh", headers={"Authorization": ""})
        assert r.status_code == 401

    async def test_wrong_slug_token_401(self, client, tenant, session):
        other = TenantModel(slug="globex", name="Globex",
                            settings={"portal": {"enabled": True}})
        session.add(other); await session.commit()
        tok = await _login(client, "acme", "jane@example.com")
        r = await client.post(f"{PORTAL}/globex/auth/refresh", headers=_hdr(tok))
        assert r.status_code == 401


class TestSubmitAutoLink:
    async def test_new_submission_links_to_existing_account(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        r = await client.post(f"{PORTAL}/acme/submit", json={
            "case_type_id": str(case_type.id), "submitter_name": "Jane",
            "submitter_email": "jane@example.com",
            "subject": "post-registration case", "description": "x",
        })
        assert r.status_code == 200, r.text
        case_id = r.json()["case_id"]
        r2 = await client.get(f"{PORTAL}/acme/account/cases", headers=_hdr(tok))
        assert case_id in {c["case_id"] for c in r2.json()["cases"]}

    async def test_submission_without_account_still_works(self, client, tenant, case_type):
        r = await client.post(f"{PORTAL}/acme/submit", json={
            "case_type_id": str(case_type.id), "submitter_name": "Anon",
            "submitter_email": "anon@example.com",
            "subject": "anonymous", "description": "x",
        })
        assert r.status_code == 200


class TestCaseDetail:
    async def test_detail_with_stage_rail(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com")
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}", headers=_hdr(tok))
        assert r.status_code == 200, r.text
        body = r.json()
        rail = body["stage_rail"]
        assert [s["id"] for s in rail] == ["submission", "review", "decision"]
        assert [s["label"] for s in rail] == ["Submission", "Review", "Decision"]  # name fallback
        assert rail[1]["current"] and rail[1]["reached"] and rail[0]["reached"]
        assert not rail[2]["reached"]

    async def test_resolved_case_rail_fully_reached(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com",
                                  stage="decision", status="resolved")
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}", headers=_hdr(tok))
        assert all(s["reached"] for s in r.json()["stage_rail"])
        assert not any(s["current"] for s in r.json()["stage_rail"])

    async def test_unlinked_case_404(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        stranger = CaseInstanceModel(
            case_type_id=case_type.id, case_type_version="1.0", status="open",
            priority="medium", tenant_id=tenant.id,
            portal_submitter_email="someone-else@example.com",
            data={"subject": "not yours"}, created_by="portal:x",
        )
        session.add(stranger); await session.commit()
        r = await client.get(f"{PORTAL}/acme/account/cases/{stranger.id}", headers=_hdr(tok))
        assert r.status_code == 404

    async def test_timeline_and_documents_reachable(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com")
        for sub in ("timeline", "documents"):
            r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/{sub}", headers=_hdr(tok))
            assert r.status_code == 200, f"{sub}: {r.text}"

    async def test_no_customer_token_401(self, client, tenant, case_type, session):
        await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com")
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}",
                             headers={"Authorization": ""})
        assert r.status_code == 401


class TestStageLabels:
    async def test_admin_sets_labels_and_rail_reflects(self, client, tenant, case_type, session):
        r = await client.patch(f"{PADMIN}/case-types/{case_type.id}/portal-display", json={
            "stage_labels": {"submission": "We got it", "review": "Being checked"},
            "expected_days": 7,
        })
        assert r.status_code == 200, r.text

        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com")
        body = (await client.get(f"{PORTAL}/acme/account/cases/{case.id}",
                                 headers=_hdr(tok))).json()
        labels = [s["label"] for s in body["stage_rail"]]
        assert labels == ["We got it", "Being checked", "Decision"]
        assert body["expected_days"] == 7

    async def test_unknown_stage_id_400(self, client, case_type):
        r = await client.patch(f"{PADMIN}/case-types/{case_type.id}/portal-display",
                               json={"stage_labels": {"bogus": "x"}})
        assert r.status_code == 400

    async def test_get_display_lists_stages(self, client, case_type):
        r = await client.get(f"{PADMIN}/case-types/{case_type.id}/portal-display")
        assert r.status_code == 200
        assert [s["id"] for s in r.json()["stages"]] == ["submission", "review", "decision"]

    async def test_display_endpoints_need_auth(self, anon_client, case_type):
        r = await anon_client.get(f"{PADMIN}/case-types/{case_type.id}/portal-display")
        assert r.status_code == 401


class TestCustomerSessions:
    async def _session_with_participant(self, session, case, identity: str | None):
        row = CaseSessionModel(
            case_id=case.id, tenant_id="acme", driver="embedded",
            provider="livekit", status="active", title="Advice call",
            started_by="user:worker-1",
            external_meeting_id=f"vx-acme-{uuid.uuid4()}",
        )
        session.add(row); await session.flush()
        if identity:
            session.add(CaseSessionParticipantModel(
                session_id=row.id, tenant_id="acme", identity=identity,
                display_name="Jane", role="guest", invited_by="worker",
            ))
        await session.commit()
        return row

    async def test_invited_session_listed(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com")
        me = (await client.get(f"{PORTAL}/acme/account", headers=_hdr(tok))).json()
        await self._session_with_participant(session, case, f"customer:{me['id']}")
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/sessions", headers=_hdr(tok))
        assert r.status_code == 200
        assert len(r.json()["sessions"]) == 1

    async def test_uninvited_session_hidden_and_unjoinable(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com")
        row = await self._session_with_participant(session, case, None)  # internal-only
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/sessions", headers=_hdr(tok))
        assert r.json()["sessions"] == []
        r2 = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/sessions/{row.id}/token",
            headers=_hdr(tok))
        assert r2.status_code == 404

    async def test_invited_customer_gets_token(self, client, tenant, case_type, session):
        from case_service.config import get_settings
        s = get_settings()
        s.livekit_url = "wss://livekit.test"
        s.livekit_api_key = "lk_test_key"
        s.livekit_api_secret = "lk_test_secret_of_sufficient_length"

        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com")
        me = (await client.get(f"{PORTAL}/acme/account", headers=_hdr(tok))).json()
        row = await self._session_with_participant(session, case, f"customer:{me['id']}")
        r = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/sessions/{row.id}/token",
            headers=_hdr(tok))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"] and body["url"]
        assert body["identity"] == f"customer:{me['id']}"


class TestOfflineIdempotency:
    """Portal v2 P2 — client_ref dedupe for offline/PWA replayed submissions."""

    def _payload(self, case_type, ref):
        return {
            "case_type_id": str(case_type.id), "submitter_name": "Offline Olive",
            "submitter_email": "olive@example.com", "subject": "queued while offline",
            "description": "x", "client_ref": ref,
        }

    async def test_replay_returns_original_case(self, client, tenant, case_type):
        ref = str(uuid.uuid4())
        r1 = await client.post(f"{PORTAL}/acme/submit", json=self._payload(case_type, ref))
        assert r1.status_code == 200, r1.text
        r2 = await client.post(f"{PORTAL}/acme/submit", json=self._payload(case_type, ref))
        assert r2.status_code == 200
        assert r2.json()["case_id"] == r1.json()["case_id"]
        assert r2.json()["tracking_token"] == r1.json()["tracking_token"]
        assert "already" in r2.json()["message"]

    async def test_distinct_refs_create_distinct_cases(self, client, tenant, case_type):
        r1 = await client.post(f"{PORTAL}/acme/submit",
                               json=self._payload(case_type, str(uuid.uuid4())))
        r2 = await client.post(f"{PORTAL}/acme/submit",
                               json=self._payload(case_type, str(uuid.uuid4())))
        assert r1.json()["case_id"] != r2.json()["case_id"]

    async def test_no_ref_still_works(self, client, tenant, case_type):
        p = self._payload(case_type, None)
        del p["client_ref"]
        r = await client.post(f"{PORTAL}/acme/submit", json=p)
        assert r.status_code == 200

    async def test_ref_replay_on_other_tenant_400(self, client, tenant, case_type, session):
        other = TenantModel(slug="globex2", name="Globex2",
                            settings={"portal": {"enabled": True,
                                                 "allowed_case_type_ids": []}})
        session.add(other); await session.commit()
        ref = str(uuid.uuid4())
        r1 = await client.post(f"{PORTAL}/acme/submit", json=self._payload(case_type, ref))
        assert r1.status_code == 200
        r2 = await client.post(f"{PORTAL}/globex2/submit", json=self._payload(case_type, ref))
        assert r2.status_code == 400


CUSTOMER_STAGES = [
    {"id": "intake", "name": "Intake", "order": 0, "steps": [
        {"id": "confirm_details", "name": "Confirm your details", "required": True,
         "step_type": "user_task", "assignment": {"strategy": "customer"},
         "customer_action": {"type": "approval", "prompt": "Please confirm the details we have on file."}},
    ]},
    {"id": "processing", "name": "Processing", "order": 1, "steps": [
        {"id": "worker_review", "name": "Worker review", "required": True,
         "step_type": "user_task", "assignment": {"strategy": "queue_based"}},
    ]},
]


@pytest_asyncio.fixture
async def action_case_type(session) -> CaseTypeModel:
    ct = CaseTypeModel(name="Customer-Action-CT", version="1.0",
                       definition_json={"stages": CUSTOMER_STAGES}, portal_enabled=True)
    session.add(ct); await session.commit(); return ct


class TestCustomerActions:
    """Portal v2 P3 — customer-assigned workflow steps."""

    async def _setup(self, client, session, tenant, action_case_type):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, action_case_type,
                                  "jane@example.com", stage="intake")
        return tok, case

    async def test_pending_action_listed(self, client, tenant, action_case_type, session):
        tok, case = await self._setup(client, session, tenant, action_case_type)
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/actions", headers=_hdr(tok))
        assert r.status_code == 200, r.text
        acts = r.json()["actions"]
        assert len(acts) == 1
        assert acts[0]["step_id"] == "confirm_details"
        assert acts[0]["type"] == "approval"
        assert "confirm the details" in acts[0]["prompt"]

    async def test_approve_completes_and_advances_stage(self, client, tenant, action_case_type, session):
        tok, case = await self._setup(client, session, tenant, action_case_type)
        r = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
            headers=_hdr(tok), json={"decision": "approved"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed"
        assert body["auto_advanced"] is True
        await session.refresh(case)
        assert case.current_stage_id == "processing"
        # and the action list is now empty (stage moved on)
        r2 = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/actions", headers=_hdr(tok))
        assert r2.json()["actions"] == []

    async def test_reject_records_and_blocks_stage(self, client, tenant, action_case_type, session):
        tok, case = await self._setup(client, session, tenant, action_case_type)
        r = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
            headers=_hdr(tok), json={"decision": "rejected", "comment": "wrong address"})
        assert r.json()["status"] == "rejected"
        assert r.json()["auto_advanced"] is False
        await session.refresh(case)
        assert case.current_stage_id == "intake"   # rejection does not advance

    async def test_double_complete_rejected_then_retry_409(self, client, tenant, action_case_type, session):
        # A rejection does NOT advance the stage, so the step is still current —
        # a second submit must 409 (the record exists), not silently overwrite.
        tok, case = await self._setup(client, session, tenant, action_case_type)
        await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
            headers=_hdr(tok), json={"decision": "rejected"})
        r = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
            headers=_hdr(tok), json={"decision": "approved"})
        assert r.status_code == 409

    async def test_replay_after_advance_404(self, client, tenant, action_case_type, session):
        # Approval advances the stage; the action no longer exists on the
        # current stage, so a replay is a uniform 404.
        tok, case = await self._setup(client, session, tenant, action_case_type)
        await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
            headers=_hdr(tok), json={"decision": "approved"})
        r = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
            headers=_hdr(tok), json={"decision": "approved"})
        assert r.status_code == 404

    async def test_worker_step_not_actionable(self, client, tenant, action_case_type, session):
        tok, case = await self._setup(client, session, tenant, action_case_type)
        r = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/worker_review/complete",
            headers=_hdr(tok), json={"decision": "approved"})
        assert r.status_code == 404

    async def test_bad_decision_400(self, client, tenant, action_case_type, session):
        tok, case = await self._setup(client, session, tenant, action_case_type)
        r = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
            headers=_hdr(tok), json={"decision": "maybe"})
        assert r.status_code == 400

    async def test_form_action_requires_fields(self, client, tenant, session):
        ct = CaseTypeModel(name="Form-Action-CT", version="1.0", portal_enabled=True,
            definition_json={"stages": [
                {"id": "info", "name": "Info", "order": 0, "steps": [
                    {"id": "bank_form", "name": "Provide details", "required": True,
                     "step_type": "user_task", "assignment": {"strategy": "customer"},
                     "customer_action": {"type": "form", "prompt": "We need this info.",
                                         "form_fields": [{"key": "iban", "label": "IBAN", "type": "text"}]}},
                ]},
                {"id": "done", "name": "Done", "order": 1, "steps": [
                    {"id": "close_out", "name": "Close", "required": True,
                     "step_type": "user_task", "assignment": {"strategy": "queue_based"}},
                ]},
            ]})
        session.add(ct); await session.commit()
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, ct, "jane@example.com", stage="info")

        r = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/bank_form/complete",
            headers=_hdr(tok), json={"data": {}})
        assert r.status_code == 400
        r2 = await client.post(
            f"{PORTAL}/acme/account/cases/{case.id}/actions/bank_form/complete",
            headers=_hdr(tok), json={"data": {"iban": "DE00 1234"}})
        assert r2.status_code == 200
        assert r2.json()["auto_advanced"] is True

    async def test_actions_hidden_on_closed_case(self, client, tenant, action_case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, action_case_type,
                                  "jane@example.com", stage="intake", status="resolved")
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/actions", headers=_hdr(tok))
        assert r.json()["actions"] == []


class TestCaseMessages:
    """Portal v2 P4 — worker ↔ customer case thread."""

    async def _case(self, client, session, tenant, case_type):
        # Portal submissions create tenant-less cases (submit_case sets no
        # tenant_id) — mirror that so the worker-side route sees the case.
        tok = await _login(client, "acme", "jane@example.com")
        case = CaseInstanceModel(
            case_type_id=case_type.id, case_type_version="1.0", status="open",
            priority="medium", portal_submitter_email="jane@example.com",
            portal_tracking_token=uuid.uuid4(), current_stage_id="review",
            data={"subject": "Thread case"}, created_by="portal:jane@example.com",
        )
        session.add(case); await session.flush()
        customer = (await session.execute(
            __import__("sqlalchemy").select(PortalCustomerModel)
            .where(PortalCustomerModel.primary_email == "jane@example.com",
                   PortalCustomerModel.tenant_id == tenant.id)
        )).scalar_one()
        session.add(PortalCustomerCaseLinkModel(customer_id=customer.id, case_id=case.id))
        await session.commit()
        return tok, case

    async def test_worker_posts_customer_reads(self, client, tenant, case_type, session):
        tok, case = await self._case(client, session, tenant, case_type)
        r = await client.post(f"/api/v1/cases/{case.id}/messages",
                              json={"body": "Hello from support"})
        assert r.status_code == 201, r.text
        r2 = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/messages", headers=_hdr(tok))
        msgs = r2.json()["messages"]
        assert len(msgs) == 1 and msgs[0]["body"] == "Hello from support"
        assert msgs[0]["mine"] is False

    async def test_customer_posts_worker_reads(self, client, tenant, case_type, session):
        tok, case = await self._case(client, session, tenant, case_type)
        r = await client.post(f"{PORTAL}/acme/account/cases/{case.id}/messages",
                              headers=_hdr(tok), json={"body": "A question from me"})
        assert r.status_code == 201, r.text
        r2 = await client.get(f"/api/v1/cases/{case.id}/messages")
        bodies = [m["body"] for m in r2.json()["messages"]]
        assert "A question from me" in bodies
        assert r2.json()["messages"][0]["author"].startswith("customer:")

    async def test_internal_note_hidden_from_portal(self, client, tenant, case_type, session):
        tok, case = await self._case(client, session, tenant, case_type)
        await client.post(f"/api/v1/cases/{case.id}/messages",
                          json={"body": "internal note", "portal_visible": False})
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/messages", headers=_hdr(tok))
        assert r.json()["messages"] == []
        r2 = await client.get(f"/api/v1/cases/{case.id}/messages")
        assert len(r2.json()["messages"]) == 1  # workers still see it

    async def test_worker_post_notifies_customer(self, client, tenant, case_type, session):
        from unittest.mock import AsyncMock, patch as _patch
        tok, case = await self._case(client, session, tenant, case_type)
        with _patch("case_service.api.routers.messages.notify_customer_of_message",
                    new_callable=AsyncMock) as mock_notify:
            await client.post(f"/api/v1/cases/{case.id}/messages", json={"body": "ping"})
            mock_notify.assert_awaited_once()
        with _patch("case_service.api.routers.messages.notify_customer_of_message",
                    new_callable=AsyncMock) as mock_notify:
            await client.post(f"/api/v1/cases/{case.id}/messages",
                              json={"body": "internal", "portal_visible": False})
            mock_notify.assert_not_awaited()

    async def test_notify_pref_roundtrip(self, client, tenant):
        tok = await _login(client, "acme", "jane@example.com")
        r = await client.get(f"{PORTAL}/acme/account", headers=_hdr(tok))
        assert r.json()["notify_email"] is True
        await client.put(f"{PORTAL}/acme/account", headers=_hdr(tok),
                         json={"notify_email": False})
        r2 = await client.get(f"{PORTAL}/acme/account", headers=_hdr(tok))
        assert r2.json()["notify_email"] is False

    async def test_unlinked_customer_404(self, client, tenant, case_type, session):
        tok, case = await self._case(client, session, tenant, case_type)
        sent, patcher = _capture_otp()
        with patcher:
            await client.post(f"{PORTAL}/acme/auth/register",
                              json={"email": "other@example.com", "display_name": "Other"})
        r = await client.post(f"{PORTAL}/acme/auth/verify-otp",
                              json={"email": "other@example.com", "otp": sent[-1]})
        other_tok = r.json()["customer_token"]
        r2 = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/messages",
                              headers=_hdr(other_tok))
        assert r2.status_code == 404

    async def test_empty_message_400(self, client, tenant, case_type, session):
        tok, case = await self._case(client, session, tenant, case_type)
        r = await client.post(f"{PORTAL}/acme/account/cases/{case.id}/messages",
                              headers=_hdr(tok), json={"body": "   "})
        assert r.status_code == 400


# 1x1 transparent PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f0300050001a5f645400000000049454e44ae426082")


class TestCsat:
    """Portal v2 P5 — post-resolution rating."""

    async def test_rate_resolved_case_once(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com",
                                  stage="decision", status="resolved")
        r = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/csat", headers=_hdr(tok))
        assert r.json() == {"rated": False, "rating": None, "can_rate": True}
        r2 = await client.post(f"{PORTAL}/acme/account/cases/{case.id}/csat",
                               headers=_hdr(tok), json={"rating": 4, "comment": "quick!"})
        assert r2.status_code == 201, r2.text
        r3 = await client.post(f"{PORTAL}/acme/account/cases/{case.id}/csat",
                               headers=_hdr(tok), json={"rating": 5})
        assert r3.status_code == 409
        r4 = await client.get(f"{PORTAL}/acme/account/cases/{case.id}/csat", headers=_hdr(tok))
        assert r4.json()["rated"] is True and r4.json()["rating"] == 4

    async def test_open_case_cannot_rate(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com")
        r = await client.post(f"{PORTAL}/acme/account/cases/{case.id}/csat",
                              headers=_hdr(tok), json={"rating": 5})
        assert r.status_code == 400

    async def test_rating_bounds(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, case_type, "jane@example.com",
                                  status="resolved")
        for bad in (0, 6):
            r = await client.post(f"{PORTAL}/acme/account/cases/{case.id}/csat",
                                  headers=_hdr(tok), json={"rating": bad})
            assert r.status_code == 400


class TestAskFeedback:
    async def test_records_feedback(self, client, tenant):
        r = await client.post(f"{PORTAL}/acme/ask/feedback",
                              json={"question": "how do I reset my password?", "helpful": True})
        assert r.status_code == 201
        from case_service.db.models import PortalAskFeedbackModel

    async def test_unknown_portal_404(self, client):
        r = await client.post(f"{PORTAL}/nope/ask/feedback",
                              json={"question": "x", "helpful": False})
        assert r.status_code == 404


class TestLogo:
    async def test_upload_and_serve(self, client, tenant, tmp_path, monkeypatch):
        import case_service.api.routers.portal as portal_mod

        class _MemStorage:
            store: dict[str, bytes] = {}
            async def put(self, key, data, content_type="application/octet-stream"):
                self.store[key] = data
            async def get(self, key):
                return self.store[key]

        mem = _MemStorage()
        monkeypatch.setattr(portal_mod, "get_storage_backend", lambda: mem)

        r = await client.post(f"{PADMIN}/tenants/acme/logo",
                              files={"file": ("logo.png", _PNG, "image/png")})
        assert r.status_code == 200, r.text
        assert r.json()["logo_url"] == "/api/v1/portal/acme/logo"

        r2 = await client.get(f"{PORTAL}/acme/logo")
        assert r2.status_code == 200
        assert r2.content == _PNG

        cfg = await client.get(f"{PORTAL}/acme")
        assert cfg.json()["logo_url"] == "/api/v1/portal/acme/logo"

    async def test_rejects_non_image(self, client, tenant):
        r = await client.post(f"{PADMIN}/tenants/acme/logo",
                              files={"file": ("evil.png", b"<script>alert(1)</script>", "image/png")})
        assert r.status_code == 400

    async def test_no_logo_404(self, client, tenant):
        r = await client.get(f"{PORTAL}/acme/logo")
        assert r.status_code == 404


class TestPortalForm:
    async def test_form_fields_served_when_configured(self, client, tenant, case_type, session):
        from case_service.db.models import FormDefinitionModel
        form = FormDefinitionModel(name="Portal Intake", version="1.0", definition_json={
            "sections": [{"id": "s1", "order": 0, "title": "S1", "fields": [
                {"id": "f1", "field_key": "policy_no", "label": "Policy number",
                 "type": "text", "required": True, "placeholder": "e.g. POL-123"},
                {"id": "f2", "field_key": "notes", "label": "Notes", "type": "textarea",
                 "required": False},
            ]}]})
        session.add(form); await session.commit()

        r0 = await client.get(f"{PORTAL}/acme/case-types/{case_type.id}/form")
        assert r0.json()["fields"] == []   # nothing configured yet

        r1 = await client.patch(f"{PADMIN}/case-types/{case_type.id}/portal-display",
                                json={"form_id": str(form.id)})
        assert r1.status_code == 200, r1.text

        r2 = await client.get(f"{PORTAL}/acme/case-types/{case_type.id}/form")
        fields = r2.json()["fields"]
        assert [f["key"] for f in fields] == ["policy_no", "notes"]
        assert fields[0]["required"] is True

    async def test_unknown_form_id_400(self, client, tenant, case_type):
        r = await client.patch(f"{PADMIN}/case-types/{case_type.id}/portal-display",
                               json={"form_id": str(uuid.uuid4())})
        assert r.status_code == 400

    async def test_extra_data_lands_in_case(self, client, tenant, case_type, session):
        tok = await _login(client, "acme", "jane@example.com")
        r = await client.post(f"{PORTAL}/acme/submit", json={
            "case_type_id": str(case_type.id), "submitter_name": "Jane",
            "submitter_email": "jane@example.com", "subject": "form case",
            "description": "x", "extra_data": {"policy_no": "POL-77"},
        })
        assert r.status_code == 200
        case = await session.get(CaseInstanceModel, uuid.UUID(r.json()["case_id"]))
        assert (case.data or {}).get("policy_no") == "POL-77"


class TestCustomerActionSideEffects:
    """Portal v2 P3 — customer completion fires the same step_complete
    connector rules as the worker path (parity check)."""

    async def test_completion_fires_step_complete_rules(self, client, tenant, action_case_type, session):
        from unittest.mock import AsyncMock, patch as _patch
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, action_case_type,
                                  "jane@example.com", stage="intake")
        with _patch("case_service.api.routers.cases.fire_outbound_rules",
                    new_callable=AsyncMock) as mock_rules:
            r = await client.post(
                f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
                headers=_hdr(tok), json={"decision": "approved"})
            assert r.status_code == 200, r.text
            events = {c.kwargs.get("trigger_event") for c in mock_rules.await_args_list}
            # step_complete (this path) + stage_exit/stage_enter (auto-advance)
            assert "step_complete" in events

    async def test_rejection_does_not_fire_step_complete(self, client, tenant, action_case_type, session):
        from unittest.mock import AsyncMock, patch as _patch
        tok = await _login(client, "acme", "jane@example.com")
        case = await _linked_case(session, tenant, action_case_type,
                                  "jane@example.com", stage="intake")
        with _patch("case_service.api.routers.cases.fire_outbound_rules",
                    new_callable=AsyncMock) as mock_rules:
            await client.post(
                f"{PORTAL}/acme/account/cases/{case.id}/actions/confirm_details/complete",
                headers=_hdr(tok), json={"decision": "rejected"})
            events = {c.kwargs.get("trigger_event") for c in mock_rules.await_args_list}
            assert "step_complete" not in events
