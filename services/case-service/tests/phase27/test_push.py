"""HELIX P27 — Push Notification tests (25 tests).

All channel HTTP calls are mocked; no real FCM/APNs/WebPush traffic.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.db.models import (
    CaseTypeModel,
    DeviceTokenModel,
    NotificationPreferenceModel,
    CaseTypeNotificationOverrideModel,
    NotificationLogModel,
)
from case_service.push.protocol import DeliveryResult, PushPayload
from case_service.push.fcm import FCMChannel
from case_service.push.apns import APNsChannel
from case_service.push.webpush import WebPushChannel
from case_service.push.service import resolve_channels, send_to_user


# ── Helpers ──────────────────────────────────────────────────────────

def _fake_user(user_id: str = "user-1", roles: list[str] | None = None):
    from case_service.auth.models import AuthenticatedUser
    return AuthenticatedUser(
        user_id=user_id,
        email=f"{user_id}@test.local",
        roles=roles or ["viewer"],
    )


# ── Protocol & channel unit tests ────────────────────────────────────

def test_01_fcm_unavailable_when_no_creds(monkeypatch):
    monkeypatch.delenv("FCM_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("FCM_SERVICE_ACCOUNT_JSON_CONTENT", raising=False)
    monkeypatch.delenv("FCM_PROJECT_ID", raising=False)
    ch = FCMChannel()
    assert ch.available is False


def test_02_apns_unavailable_when_no_creds(monkeypatch):
    monkeypatch.delenv("APNS_KEY_FILE", raising=False)
    monkeypatch.delenv("APNS_KEY_CONTENT", raising=False)
    monkeypatch.delenv("APNS_KEY_ID", raising=False)
    monkeypatch.delenv("APNS_TEAM_ID", raising=False)
    monkeypatch.delenv("APNS_BUNDLE_ID", raising=False)
    ch = APNsChannel()
    assert ch.available is False


def test_03_webpush_unavailable_when_no_creds(monkeypatch):
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    ch = WebPushChannel()
    assert ch.available is False


def test_04_webpush_available_when_creds_set(monkeypatch):
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "fake-priv")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "fake-pub")
    ch = WebPushChannel()
    assert ch.available is True
    assert ch.get_public_key() == "fake-pub"


@pytest.mark.asyncio
async def test_05_fcm_returns_failure_when_unavailable():
    ch = FCMChannel()
    ch._sa = None  # force unavailable
    result = await ch.send("some-token", PushPayload(title="T", body="B"))
    assert result.success is False
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_06_apns_returns_failure_when_unavailable():
    ch = APNsChannel()
    ch._key_pem = None  # force unavailable
    result = await ch.send("device-token", PushPayload(title="T", body="B"))
    assert result.success is False


@pytest.mark.asyncio
async def test_07_webpush_returns_failure_when_unavailable():
    ch = WebPushChannel()
    ch._private_key = None
    result = await ch.send("{}", PushPayload(title="T", body="B"))
    assert result.success is False


@pytest.mark.asyncio
async def test_08_fcm_success_via_mock():
    ch = FCMChannel()
    ch._sa = {"project_id": "proj", "client_email": "x@x.iam", "private_key": "k"}
    ch._project_id = "proj"
    ch._access_token = "tok"
    ch._token_expiry = 9999999999.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await ch.send("device-abc", PushPayload(title="Hi", body="World"))

    assert result.success is True
    assert result.token_prefix == "device-a"


@pytest.mark.asyncio
async def test_09_fcm_deactivates_on_invalid_registration():
    ch = FCMChannel()
    ch._sa = {"project_id": "proj", "client_email": "x@x.iam", "private_key": "k"}
    ch._project_id = "proj"
    ch._access_token = "tok"
    ch._token_expiry = 9999999999.0

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {"error": {"message": "UNREGISTERED: token invalid"}}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await ch.send("deadtoken1234", PushPayload(title="T", body="B"))

    assert result.success is False
    assert result.should_deactivate_token is True


@pytest.mark.asyncio
async def test_10_webpush_deactivates_on_410():
    import sys
    ch = WebPushChannel()
    ch._private_key = "fake"
    ch._public_key = "fake"

    # Inject a fake pywebpush so the local import inside send() resolves
    fake_mod = MagicMock()
    fake_mod.webpush = MagicMock(side_effect=Exception("410 Gone"))
    fake_mod.WebPushException = Exception
    with patch.dict(sys.modules, {"pywebpush": fake_mod}):
        result = await ch.send("{}", PushPayload(title="T", body="B"))

    assert result.success is False
    assert result.should_deactivate_token is True


# ── DB model + API tests ──────────────────────────────────────────────

@pytest_asyncio.fixture
async def ct(session):
    c = CaseTypeModel(
        name="P27-Type", version="1.0",
        lifecycle_process_id="lp-p27",
        definition_json={"stages": []},
    )
    session.add(c)
    await session.flush()
    return c


@pytest.mark.asyncio
async def test_11_register_device(client: AsyncClient):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_user()

    resp = await client.post("/api/v1/push/devices", json={
        "channel": "fcm",
        "token": "fcm-token-abc123",
        "platform": "android",
        "label": "Test Phone",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["channel"] == "fcm"
    assert data["token_prefix"] == "fcm-toke"   # first 8 chars
    assert "token" not in data                   # full token never returned


@pytest.mark.asyncio
async def test_12_register_same_device_twice_is_idempotent(client: AsyncClient, session):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_user("u2")

    payload = {"channel": "webpush", "token": "wp-tok-xyz", "platform": "web"}
    r1 = await client.post("/api/v1/push/devices", json=payload)
    r2 = await client.post("/api/v1/push/devices", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


@pytest.mark.asyncio
async def test_13_list_own_devices(client: AsyncClient, session):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    user = _fake_user("listuser")
    app.dependency_overrides[get_current_user] = lambda: user

    await client.post("/api/v1/push/devices", json={"channel": "fcm", "token": "tok-list-1"})
    await client.post("/api/v1/push/devices", json={"channel": "apns", "token": "tok-list-2"})

    resp = await client.get("/api/v1/push/devices")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_14_deregister_device(client: AsyncClient, session):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    user = _fake_user("deluser")
    app.dependency_overrides[get_current_user] = lambda: user

    r = await client.post("/api/v1/push/devices", json={"channel": "fcm", "token": "tok-del"})
    dev_id = r.json()["id"]

    del_r = await client.delete(f"/api/v1/push/devices/{dev_id}")
    assert del_r.status_code == 204

    list_r = await client.get("/api/v1/push/devices")
    assert all(d["id"] != dev_id for d in list_r.json())


@pytest.mark.asyncio
async def test_15_cannot_delete_other_users_device(client: AsyncClient, session):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app

    app.dependency_overrides[get_current_user] = lambda: _fake_user("owner")
    r = await client.post("/api/v1/push/devices", json={"channel": "fcm", "token": "tok-owner"})
    dev_id = r.json()["id"]

    app.dependency_overrides[get_current_user] = lambda: _fake_user("attacker")
    del_r = await client.delete(f"/api/v1/push/devices/{dev_id}")
    assert del_r.status_code == 404


@pytest.mark.asyncio
async def test_16_upsert_preference(client: AsyncClient):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_user("pref-user")

    resp = await client.put("/api/v1/push/preferences/case.assigned", json={
        "channels": ["fcm", "webpush"],
        "enabled": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["channels"] == ["fcm", "webpush"]
    assert data["enabled"] is True


@pytest.mark.asyncio
async def test_17_preference_disabled_blocks_all_channels(client: AsyncClient):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_user("pref-user2")

    await client.put("/api/v1/push/preferences/case.updated", json={
        "channels": ["fcm"],
        "enabled": False,
    })
    resp = await client.get("/api/v1/push/preferences")
    prefs = resp.json()
    pref = next(p for p in prefs if p["event_type"] == "case.updated")
    assert pref["enabled"] is False


@pytest.mark.asyncio
async def test_18_invalid_channel_rejected(client: AsyncClient):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_user()

    resp = await client.put("/api/v1/push/preferences/case.created", json={
        "channels": ["telegram"],   # not a valid channel
        "enabled": True,
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_19_admin_can_list_all_devices(client: AsyncClient):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    admin = _fake_user("admin-user", roles=["admin"])
    app.dependency_overrides[get_current_user] = lambda: admin

    resp = await client.get("/api/v1/push/admin/devices")
    assert resp.status_code == 200
    assert "devices" in resp.json()


@pytest.mark.asyncio
async def test_20_vapid_public_key_returned(client: AsyncClient):
    from case_service.auth.dependencies import get_current_user
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_user()

    with patch("case_service.api.routers.push.get_vapid_public_key", return_value="BPub123key"):
        resp = await client.get("/api/v1/push/vapid-public-key")

    assert resp.status_code == 200
    assert resp.json()["vapid_public_key"] == "BPub123key"


# ── Preference resolution unit tests ─────────────────────────────────

@pytest.mark.asyncio
async def test_21_resolve_channels_global_default(session):
    channels = await resolve_channels(session, "new-user", "case.created")
    assert set(channels) == {"fcm", "apns", "webpush"}


@pytest.mark.asyncio
async def test_22_resolve_channels_respects_user_pref(session):
    pref = NotificationPreferenceModel(
        user_id="pref-resolve-user",
        event_type="case.created",
        channels=["fcm"],
        enabled=True,
    )
    session.add(pref)
    await session.flush()

    channels = await resolve_channels(session, "pref-resolve-user", "case.created")
    assert channels == ["fcm"]


@pytest.mark.asyncio
async def test_23_resolve_channels_disabled_returns_empty(session):
    pref = NotificationPreferenceModel(
        user_id="disabled-user",
        event_type="case.closed",
        channels=["fcm"],
        enabled=False,
    )
    session.add(pref)
    await session.flush()

    channels = await resolve_channels(session, "disabled-user", "case.closed")
    assert channels == []


@pytest.mark.asyncio
async def test_24_case_type_override_takes_priority(session, ct):
    pref = NotificationPreferenceModel(
        user_id="override-user",
        event_type="case.assigned",
        channels=["fcm"],
        enabled=True,
    )
    override = CaseTypeNotificationOverrideModel(
        case_type_id=ct.id,
        event_type="case.assigned",
        channels=["webpush"],
        enabled=True,
    )
    session.add(pref)
    session.add(override)
    await session.flush()

    channels = await resolve_channels(session, "override-user", "case.assigned", ct.id)
    assert channels == ["webpush"]   # override wins


@pytest.mark.asyncio
async def test_25_send_to_user_marks_stale_token_inactive(session):
    """When a channel reports should_deactivate_token, the device is marked inactive."""
    device = DeviceTokenModel(
        user_id="stale-user",
        channel="fcm",
        token="stale-token-xyz",
        is_active=True,
    )
    session.add(device)
    await session.flush()

    mock_channel = MagicMock()
    mock_channel.channel_name = "fcm"
    mock_channel.available = True
    mock_channel.send = AsyncMock(return_value=DeliveryResult(
        success=False, channel="fcm", token_prefix="stale-to",
        error="UNREGISTERED", should_deactivate_token=True,
    ))

    with patch.dict("case_service.push.service._CHANNELS", {"fcm": mock_channel}):
        await send_to_user(session, "stale-user", "case.closed",
                           PushPayload(title="T", body="B"))

    await session.refresh(device)
    assert device.is_active is False
