"""Tests for P51 HxConnect — Communications (Twilio SMS + Slack)."""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import ConnectorRegistryModel, SmsMessageModel, SlackNotificationModel
from case_service.hxbridge.encryption import encrypt_credentials

from tests.conftest import client, session, deploy_case_type, create_case  # type: ignore[attr-defined]


# ── helpers ───────────────────────────────────────────────────────────────────

async def _reg_twilio(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="Twilio Test", connector_type="twilio",
        config={"from_number": "+10000000000"},
        credentials=encrypt_credentials({"account_sid": "ACtest", "auth_token": "token"}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _reg_slack(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="Slack Test", connector_type="slack",
        config={"default_channel": "#ops"},
        credentials=encrypt_credentials({"webhook_url": "https://hooks.slack.com/test"}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _sms_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="SMS Case", definition_json={
        "stages": [{"id": "s1", "name": "Notify", "order": 1, "steps": [
            {"id": "sms_step", "name": "Send SMS", "step_type": "sms_send", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


async def _slack_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="Slack Case", definition_json={
        "stages": [{"id": "s1", "name": "Alert", "order": 1, "steps": [
            {"id": "slack_step", "name": "Send Slack", "step_type": "slack_notify", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


# ── SMS list ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_sms_empty(client: AsyncClient):
    case = await _sms_case(client)
    r = await client.get(f"/api/v1/comms/sms/cases/{case['id']}/messages")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_sms_no_connector_returns_400(client: AsyncClient):
    case = await _sms_case(client)
    r = await client.post(f"/api/v1/comms/sms/cases/{case['id']}/send",
                          json={"step_id": "sms_step", "to_number": "+447700900000", "body": "Hello"})
    assert r.status_code in (400, 502)


@pytest.mark.asyncio
async def test_sms_unknown_case_returns_400(client: AsyncClient, session: AsyncSession):
    await _reg_twilio(session); await session.commit()
    r = await client.post(f"/api/v1/comms/sms/cases/{uuid.uuid4()}/send",
                          json={"step_id": "sms_step", "to_number": "+447700900000", "body": "Hello"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_sms_record_created_and_retrievable(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_twilio(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _sms_case(client)

    row = SmsMessageModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="sms_step",
        connector_id=reg.id, provider="twilio",
        to_number="+447700900001", body="Test message",
        status="sent", message_sid="SM123",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/comms/sms/cases/{case['id']}/messages")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["message_sid"] == "SM123"
    assert rows[0]["to_number"] == "+447700900001"


@pytest.mark.asyncio
async def test_sms_failed_record_shows_error(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_twilio(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _sms_case(client)

    row = SmsMessageModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="sms_step",
        connector_id=reg.id, provider="twilio",
        to_number="+447700900002", body="Fail",
        status="failed", error="Invalid phone number",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/comms/sms/cases/{case['id']}/messages")
    assert r.status_code == 200
    assert r.json()[0]["error"] == "Invalid phone number"


@pytest.mark.asyncio
async def test_multiple_sms_same_case(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_twilio(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _sms_case(client)

    for i in range(3):
        session.add(SmsMessageModel(
            tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id=f"sms_step_{i}",
            connector_id=reg.id, provider="twilio",
            to_number=f"+4477009000{i:02d}", body=f"Message {i}", status="sent",
        ))
    await session.commit()

    r = await client.get(f"/api/v1/comms/sms/cases/{case['id']}/messages")
    assert r.status_code == 200 and len(r.json()) == 3


@pytest.mark.asyncio
async def test_sms_connector_type_isolation(client: AsyncClient, session: AsyncSession):
    """Slack connector not used for SMS."""
    await _reg_slack(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _sms_case(client)
    r = await client.post(f"/api/v1/comms/sms/cases/{case['id']}/send",
                          json={"step_id": "sms_step", "to_number": "+447700900000", "body": "Hi"})
    assert r.status_code in (400, 502)


# ── Slack list ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_slack_empty(client: AsyncClient):
    case = await _slack_case(client)
    r = await client.get(f"/api/v1/comms/slack/cases/{case['id']}/notifications")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_slack_no_connector_returns_400(client: AsyncClient):
    case = await _slack_case(client)
    r = await client.post(f"/api/v1/comms/slack/cases/{case['id']}/send",
                          json={"step_id": "slack_step", "message": "Case update"})
    assert r.status_code in (400, 502)


@pytest.mark.asyncio
async def test_slack_unknown_case_returns_400(client: AsyncClient, session: AsyncSession):
    await _reg_slack(session); await session.commit()
    r = await client.post(f"/api/v1/comms/slack/cases/{uuid.uuid4()}/send",
                          json={"step_id": "slack_step", "message": "Hello"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_slack_record_created_and_retrievable(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_slack(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _slack_case(client)

    row = SlackNotificationModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="slack_step",
        connector_id=reg.id, channel="#ops",
        message="Case approved", blocks=[], status="sent",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/comms/slack/cases/{case['id']}/notifications")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["message"] == "Case approved"
    assert rows[0]["channel"] == "#ops"


@pytest.mark.asyncio
async def test_slack_failed_record_shows_error(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_slack(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _slack_case(client)

    row = SlackNotificationModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="slack_step",
        connector_id=reg.id, channel=None,
        message="Fail", blocks=[], status="failed", error="invalid_payload",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/comms/slack/cases/{case['id']}/notifications")
    assert r.json()[0]["error"] == "invalid_payload"


@pytest.mark.asyncio
async def test_slack_with_channel(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_slack(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _slack_case(client)

    row = SlackNotificationModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="slack_step",
        connector_id=reg.id, channel="#fraud-alerts",
        message="Suspicious activity detected", blocks=[], status="sent",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/comms/slack/cases/{case['id']}/notifications")
    assert r.json()[0]["channel"] == "#fraud-alerts"


@pytest.mark.asyncio
async def test_multiple_slack_same_case(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_slack(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _slack_case(client)

    for i in range(4):
        session.add(SlackNotificationModel(
            tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id=f"slack_step_{i}",
            connector_id=reg.id, channel="#ops",
            message=f"Notification {i}", blocks=[], status="sent",
        ))
    await session.commit()

    r = await client.get(f"/api/v1/comms/slack/cases/{case['id']}/notifications")
    assert r.status_code == 200 and len(r.json()) == 4


@pytest.mark.asyncio
async def test_slack_connector_type_isolation(client: AsyncClient, session: AsyncSession):
    """Twilio connector not used for Slack."""
    await _reg_twilio(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _slack_case(client)
    r = await client.post(f"/api/v1/comms/slack/cases/{case['id']}/send",
                          json={"step_id": "slack_step", "message": "Hello"})
    assert r.status_code in (400, 502)


# ── Connectors endpoint ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_comms_connectors_empty(client: AsyncClient):
    r = await client.get("/api/v1/comms/connectors")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_list_comms_connectors_returns_twilio_and_slack(client: AsyncClient, session: AsyncSession):
    await _reg_twilio(session, tenant_id="default")
    await _reg_slack(session, tenant_id="default")
    await session.commit()

    r = await client.get("/api/v1/comms/connectors")
    assert r.status_code == 200
    types = {c["type"] for c in r.json()}
    assert "twilio" in types
    assert "slack" in types
