"""Tests for P49 HxConnect — Identity (Onfido) & E-Sign (DocuSign)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    ConnectorRegistryModel,
    ESignRequestModel,
    IdentityVerificationModel,
)
from case_service.hxbridge.connectors.onfido_connector import OnfidoConnector
from case_service.hxbridge.connectors.docusign_connector import DocuSignConnector
from case_service.hxbridge.encryption import encrypt_credentials

from tests.conftest import client, session, deploy_case_type, create_case  # type: ignore[attr-defined]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _onfido_sig(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _docusign_sig(payload: bytes, key: str) -> str:
    return base64.b64encode(hmac.new(key.encode(), payload, hashlib.sha256).digest()).decode()


async def _reg_onfido(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="Onfido Test", connector_type="onfido",
        config={"region": "EU"},
        credentials=encrypt_credentials({"api_token": "test_token", "webhook_token": "wh_secret"}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _reg_docusign(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="DocuSign Test", connector_type="docusign",
        config={"base_url": "https://demo.docusign.net/restapi"},
        credentials=encrypt_credentials({"access_token": "test_token", "account_id": "acct1", "hmac_key": "ds_secret"}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _kyc_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="KYC Case", definition_json={
        "stages": [{"id": "s1", "name": "Verification", "order": 1, "steps": [
            {"id": "id_step", "name": "Verify Identity", "step_type": "identity_verify", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


async def _esign_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="ESign Case", definition_json={
        "stages": [{"id": "s1", "name": "Signing", "order": 1, "steps": [
            {"id": "sign_step", "name": "Sign Document", "step_type": "esign_request", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


# ── Unit — Onfido HMAC ────────────────────────────────────────────────────────

class TestOnfidoHMAC:
    def _conn(self) -> OnfidoConnector:
        return OnfidoConnector(config={}, credentials={"api_token": "x", "webhook_token": "wh_secret"})

    def test_valid(self):
        c = self._conn(); p = b'{"payload":{"action":"check.completed"}}'
        assert c.verify_webhook(p, _onfido_sig(p, "wh_secret")) is True

    def test_tampered(self):
        c = self._conn(); p = b'{"payload":{}}'
        sig = _onfido_sig(p, "wh_secret")
        assert c.verify_webhook(b'{"payload":{"extra":1}}', sig) is False

    def test_wrong_secret(self):
        c = self._conn(); p = b'{"payload":{}}'
        assert c.verify_webhook(p, _onfido_sig(p, "wrong")) is False

    def test_no_secret_returns_false(self):
        c = OnfidoConnector(config={}, credentials={"api_token": "x"})
        assert c.verify_webhook(b"p", "sig") is False


# ── Unit — DocuSign HMAC ──────────────────────────────────────────────────────

class TestDocuSignHMAC:
    def _conn(self) -> DocuSignConnector:
        return DocuSignConnector(config={}, credentials={"access_token": "x", "account_id": "a", "hmac_key": "ds_secret"})

    def test_valid(self):
        c = self._conn(); p = b'{"event":"envelope-completed"}'
        assert c.verify_webhook(p, _docusign_sig(p, "ds_secret")) is True

    def test_wrong_key(self):
        c = self._conn(); p = b'{"event":"envelope-completed"}'
        assert c.verify_webhook(p, _docusign_sig(p, "bad_key")) is False

    def test_no_key_returns_false(self):
        c = DocuSignConnector(config={}, credentials={"access_token": "x", "account_id": "a"})
        assert c.verify_webhook(b"p", "sig") is False


# ── API — Identity verification ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_verifications_empty(client: AsyncClient):
    case = await _kyc_case(client)
    r = await client.get(f"/api/v1/identity/cases/{case['id']}/verifications")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_verify_no_connector_returns_400(client: AsyncClient):
    case = await _kyc_case(client)
    r = await client.post(f"/api/v1/identity/cases/{case['id']}/verify",
                          json={"step_id": "id_step", "first_name": "Alice"})
    assert r.status_code in (400, 502)


@pytest.mark.asyncio
async def test_verify_unknown_case_returns_400(client: AsyncClient, session: AsyncSession):
    await _reg_onfido(session); await session.commit()
    r = await client.post(f"/api/v1/identity/cases/{uuid.uuid4()}/verify",
                          json={"step_id": "id_step", "first_name": "Alice"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_verification_row_created(client: AsyncClient, session: AsyncSession):
    reg = await _reg_onfido(session)
    case = await _kyc_case(client); await session.commit()

    iv = IdentityVerificationModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="id_step",
        connector_id=reg.id, provider="onfido", check_id="chk_abc",
        applicant_id="app_xyz", verification_url="https://id.onfido.com/?token=tok",
        status="pending",
    )
    session.add(iv); await session.commit()

    r = await client.get(f"/api/v1/identity/cases/{case['id']}/verifications")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert "check_id" not in rows[0]     # check_id intentionally not in response (PII protection)
    assert rows[0]["status"] == "pending"
    assert rows[0]["verification_url"] == "https://id.onfido.com/?token=tok"


# ── API — Onfido webhook ──────────────────────────────────────────────────────

def _onfido_webhook(check_id: str, action: str = "check.completed", result: str = "clear") -> bytes:
    return json.dumps({
        "payload": {
            "resource_type": "check",
            "action": action,
            "object": {"id": check_id, "result": result},
        }
    }).encode()


@pytest.mark.asyncio
async def test_onfido_webhook_clear_completes_step(client: AsyncClient, session: AsyncSession):
    reg = await _reg_onfido(session)
    case = await _kyc_case(client); await session.commit()

    iv = IdentityVerificationModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="id_step",
        connector_id=reg.id, provider="onfido", check_id="chk_clear",
        status="pending",
    )
    session.add(iv); await session.commit()

    payload = _onfido_webhook("chk_clear", result="clear")
    sig = _onfido_sig(payload, "wh_secret")
    r = await client.post("/api/v1/identity/webhooks/onfido", content=payload,
                          headers={"content-type": "application/json", "x-sha2-signature": sig})
    assert r.status_code == 200

    await session.refresh(iv)
    assert iv.status == "complete"
    assert iv.result == "clear"
    assert iv.result_hash is not None


@pytest.mark.asyncio
async def test_onfido_webhook_consider_does_not_auto_complete(client: AsyncClient, session: AsyncSession):
    reg = await _reg_onfido(session)
    case = await _kyc_case(client); await session.commit()

    iv = IdentityVerificationModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="id_step",
        connector_id=reg.id, provider="onfido", check_id="chk_consider",
        status="pending",
    )
    session.add(iv); await session.commit()

    payload = _onfido_webhook("chk_consider", result="consider")
    sig = _onfido_sig(payload, "wh_secret")
    r = await client.post("/api/v1/identity/webhooks/onfido", content=payload,
                          headers={"content-type": "application/json", "x-sha2-signature": sig})
    assert r.status_code == 200

    await session.refresh(iv)
    assert iv.status == "complete"
    assert iv.result == "consider"


@pytest.mark.asyncio
async def test_onfido_invalid_hmac_rejected(client: AsyncClient, session: AsyncSession):
    reg = await _reg_onfido(session)
    case = await _kyc_case(client); await session.commit()

    iv = IdentityVerificationModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="id_step",
        connector_id=reg.id, provider="onfido", check_id="chk_badsig",
        status="pending",
    )
    session.add(iv); await session.commit()

    payload = _onfido_webhook("chk_badsig")
    r = await client.post("/api/v1/identity/webhooks/onfido", content=payload,
                          headers={"content-type": "application/json", "x-sha2-signature": "badsig"})
    assert r.status_code == 400
    await session.refresh(iv)
    assert iv.status == "pending"   # unchanged


@pytest.mark.asyncio
async def test_onfido_webhook_unknown_check_ok(client: AsyncClient):
    payload = _onfido_webhook("chk_unknown")
    r = await client.post("/api/v1/identity/webhooks/onfido", content=payload,
                          headers={"content-type": "application/json"})
    assert r.status_code == 200


# ── API — E-sign ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_esign_empty(client: AsyncClient):
    case = await _esign_case(client)
    r = await client.get(f"/api/v1/esign/cases/{case['id']}/requests")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_esign_no_connector_returns_400(client: AsyncClient):
    case = await _esign_case(client)
    r = await client.post(f"/api/v1/esign/cases/{case['id']}/send",
                          json={"step_id": "sign_step", "signer_email": "a@b.com", "signer_name": "Alice"})
    assert r.status_code in (400, 502)


@pytest.mark.asyncio
async def test_esign_unknown_case_returns_400(client: AsyncClient, session: AsyncSession):
    await _reg_docusign(session); await session.commit()
    r = await client.post(f"/api/v1/esign/cases/{uuid.uuid4()}/send",
                          json={"step_id": "sign_step", "signer_email": "a@b.com"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_esign_row_retrievable(client: AsyncClient, session: AsyncSession):
    reg = await _reg_docusign(session)
    case = await _esign_case(client); await session.commit()

    es = ESignRequestModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="sign_step",
        connector_id=reg.id, provider="docusign",
        envelope_id="env_abc",
        signing_url="https://demo.docusign.net/sign?token=tok",
        document_name="Loan Agreement", signer_email="a@b.com",
        status="sent",
    )
    session.add(es); await session.commit()

    r = await client.get(f"/api/v1/esign/cases/{case['id']}/requests")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["document_name"] == "Loan Agreement"


# ── API — DocuSign webhook ────────────────────────────────────────────────────

def _ds_webhook(envelope_id: str, event: str = "envelope-completed") -> bytes:
    return json.dumps({"event": event, "data": {"envelopeId": envelope_id}}).encode()


@pytest.mark.asyncio
async def test_docusign_completed_updates_status(client: AsyncClient, session: AsyncSession):
    reg = await _reg_docusign(session)
    case = await _esign_case(client); await session.commit()

    es = ESignRequestModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="sign_step",
        connector_id=reg.id, provider="docusign",
        envelope_id="env_done", status="sent",
    )
    session.add(es); await session.commit()

    payload = _ds_webhook("env_done")
    sig = _docusign_sig(payload, "ds_secret")
    r = await client.post("/api/v1/esign/webhooks/docusign", content=payload,
                          headers={"content-type": "application/json", "x-docusign-signature-1": sig})
    assert r.status_code == 200

    await session.refresh(es)
    assert es.status == "completed"
    assert es.signed_at is not None


@pytest.mark.asyncio
async def test_docusign_declined_updates_status(client: AsyncClient, session: AsyncSession):
    reg = await _reg_docusign(session)
    case = await _esign_case(client); await session.commit()

    es = ESignRequestModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="sign_step",
        connector_id=reg.id, provider="docusign",
        envelope_id="env_dec", status="sent",
    )
    session.add(es); await session.commit()

    payload = _ds_webhook("env_dec", "envelope-declined")
    sig = _docusign_sig(payload, "ds_secret")
    r = await client.post("/api/v1/esign/webhooks/docusign", content=payload,
                          headers={"content-type": "application/json", "x-docusign-signature-1": sig})
    assert r.status_code == 200
    await session.refresh(es)
    assert es.status == "declined"


@pytest.mark.asyncio
async def test_docusign_invalid_hmac_rejected(client: AsyncClient, session: AsyncSession):
    reg = await _reg_docusign(session)
    case = await _esign_case(client); await session.commit()

    es = ESignRequestModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="sign_step",
        connector_id=reg.id, provider="docusign",
        envelope_id="env_bad", status="sent",
    )
    session.add(es); await session.commit()

    payload = _ds_webhook("env_bad")
    r = await client.post("/api/v1/esign/webhooks/docusign", content=payload,
                          headers={"content-type": "application/json", "x-docusign-signature-1": "badsig"})
    assert r.status_code == 400
    await session.refresh(es)
    assert es.status == "sent"


@pytest.mark.asyncio
async def test_docusign_unknown_envelope_ok(client: AsyncClient):
    payload = _ds_webhook("env_unknown")
    r = await client.post("/api/v1/esign/webhooks/docusign", content=payload,
                          headers={"content-type": "application/json"})
    assert r.status_code == 200
