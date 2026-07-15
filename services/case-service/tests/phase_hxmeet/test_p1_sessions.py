"""HxMeet P1 — off-platform case sessions (Teams/Zoom/Meet/generic connectors).

Pins: session lifecycle on a case (start → active → ended/cancelled), the
first-party meeting-connector allowlist (fail closed), provider resolution
precedence (explicit → tenant default → only-enabled), 404 anti-oracle on
unknown cases/sessions, 501 on the not-yet-built embedded driver, and 502
surfacing of provider failures.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from case_service.db.models import ConnectorRegistryModel, TenantModel
from tests.conftest import create_case, deploy_case_type

MEET = "/api/v1/meet"


class _FakeMeetingConnector:
    """Stands in for any provider connector — no network."""

    def __init__(self, join_url="https://meet.example/j/123", meeting_id="ext-123", fail=False):
        self._join_url = join_url
        self._meeting_id = meeting_id
        self._fail = fail

    async def execute(self, input_data: dict) -> dict:
        if self._fail:
            raise RuntimeError("provider says no")
        return {
            "external_meeting_id": self._meeting_id,
            "join_url": self._join_url,
            "provider": "fake",
        }

    async def test(self) -> bool:
        return True


def _patch_connector(**kwargs):
    return patch(
        "case_service.meet.service.get_connector",
        return_value=_FakeMeetingConnector(**kwargs),
    )


@pytest_asyncio.fixture
async def teams_connector(session) -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="Teams (test)", connector_type="teams", tenant_id="default",
        config={"organizer_user_id": "organizer@test"},
        credentials={"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        enabled=True,
    )
    session.add(row); await session.commit(); return row


@pytest_asyncio.fixture
async def zoom_connector(session) -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="Zoom (test)", connector_type="zoom", tenant_id="default",
        config={}, credentials={"account_id": "a", "client_id": "c", "client_secret": "s"},
        enabled=True,
    )
    session.add(row); await session.commit(); return row


async def _case(client) -> dict:
    ct = await deploy_case_type(client, name=f"Meet CT {uuid.uuid4().hex[:6]}")
    return await create_case(client, ct["id"])


class TestStartSession:
    async def test_start_creates_active_session_with_join_url(self, client, teams_connector):
        case = await _case(client)
        with _patch_connector():
            r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={"title": "Kickoff"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "active"
        assert body["driver"] == "off_platform"
        assert body["provider"] == "teams"
        assert body["join_url"] == "https://meet.example/j/123"
        assert body["external_meeting_id"] == "ext-123"
        assert body["title"] == "Kickoff"

    async def test_listed_on_case(self, client, teams_connector):
        case = await _case(client)
        with _patch_connector():
            await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        r = await client.get(f"{MEET}/cases/{case['id']}/sessions")
        assert r.status_code == 200
        assert len(r.json()["sessions"]) == 1

    async def test_unknown_case_404(self, client, teams_connector):
        with _patch_connector():
            r = await client.post(f"{MEET}/cases/{uuid.uuid4()}/sessions", json={})
        assert r.status_code == 404

    async def test_no_connector_configured_400(self, client):
        case = await _case(client)
        r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        assert r.status_code == 400

    async def test_provider_error_502(self, client, teams_connector):
        case = await _case(client)
        with _patch_connector(fail=True):
            r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        assert r.status_code == 502

    async def test_embedded_driver_not_built_501(self, client, teams_connector):
        from case_service.config import get_settings
        case = await _case(client)
        s = get_settings()
        prior = s.meet_driver
        s.meet_driver = "embedded"
        try:
            r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        finally:
            s.meet_driver = prior
        assert r.status_code == 501

    async def test_unknown_provider_400(self, client, teams_connector):
        case = await _case(client)
        with _patch_connector():
            r = await client.post(f"{MEET}/cases/{case['id']}/sessions",
                                  json={"provider": "skype"})
        assert r.status_code == 400

    async def test_bad_connector_id_422(self, client, teams_connector):
        case = await _case(client)
        r = await client.post(f"{MEET}/cases/{case['id']}/sessions",
                              json={"connector_id": "not-a-uuid"})
        assert r.status_code == 422


class TestProviderResolution:
    async def test_explicit_provider_wins(self, client, teams_connector, zoom_connector):
        case = await _case(client)
        with _patch_connector():
            r = await client.post(f"{MEET}/cases/{case['id']}/sessions",
                                  json={"provider": "zoom"})
        assert r.status_code == 201
        assert r.json()["provider"] == "zoom"

    async def test_tenant_default_provider(self, client, session, teams_connector, zoom_connector):
        t = TenantModel(slug="default", name="Default",
                        settings={"meet": {"provider": "zoom"}})
        session.add(t); await session.commit()
        case = await _case(client)
        with _patch_connector():
            r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        assert r.status_code == 201
        assert r.json()["provider"] == "zoom"

    async def test_disabled_connector_not_used(self, client, session, teams_connector):
        teams_connector.enabled = False
        session.add(teams_connector); await session.commit()
        case = await _case(client)
        r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        assert r.status_code == 400

    async def test_null_tenant_connector_is_platform_shared(self, client, session):
        """hxbridge registers connectors without a tenant — those serve every tenant."""
        session.add(ConnectorRegistryModel(
            name="Teams (shared)", connector_type="teams", tenant_id=None,
            config={"organizer_user_id": "organizer@test"},
            credentials={"tenant_id": "t", "client_id": "c", "client_secret": "s"},
            enabled=True,
        ))
        await session.commit()
        case = await _case(client)
        with _patch_connector():
            r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        assert r.status_code == 201
        assert r.json()["provider"] == "teams"

    async def test_non_meeting_connector_never_drives_a_session(self, client, session):
        """Fail-closed allowlist: an enabled slack connector is not a meeting provider."""
        session.add(ConnectorRegistryModel(
            name="Slack (test)", connector_type="slack", tenant_id="default",
            config={}, credentials={"webhook_url": "https://hooks.slack test"},
            enabled=True,
        ))
        await session.commit()
        case = await _case(client)
        r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        assert r.status_code == 400


class TestEnforceMode:
    async def test_meet_start_registered_with_hxguard(self, client, teams_connector):
        """Enforce mode: meet.start must be a known case action (unknown = fail-closed
        404 for everyone, the live incident this test pins) — and an unrelated
        non-admin still gets the 404 anti-oracle."""
        from case_service.auth.jwt_handler import create_dev_token
        from case_service.config import get_settings
        case = await _case(client)
        s = get_settings()
        prior = s.hxguard_case_enforcement
        s.hxguard_case_enforcement = "enforce"
        try:
            # admin bypass works only because the action is registered
            with _patch_connector():
                r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
            assert r.status_code == 201

            viewer = create_dev_token(
                user_id=str(uuid.uuid4()), username="outsider", roles=["viewer"],
                secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
            )
            with _patch_connector():
                r2 = await client.post(
                    f"{MEET}/cases/{case['id']}/sessions", json={},
                    headers={"Authorization": f"Bearer {viewer}"},
                )
            assert r2.status_code == 404  # anti-oracle, not 403
        finally:
            s.hxguard_case_enforcement = prior


class TestEndSession:
    async def test_end_marks_ended(self, client, teams_connector):
        case = await _case(client)
        with _patch_connector():
            sid = (await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})).json()["id"]
        r = await client.post(f"{MEET}/sessions/{sid}/end")
        assert r.status_code == 200
        assert r.json()["status"] == "ended"
        assert r.json()["ended_at"]

    async def test_cancel(self, client, teams_connector):
        case = await _case(client)
        with _patch_connector():
            sid = (await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})).json()["id"]
        r = await client.post(f"{MEET}/sessions/{sid}/end?cancelled=true")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    async def test_end_is_idempotent(self, client, teams_connector):
        case = await _case(client)
        with _patch_connector():
            sid = (await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})).json()["id"]
        await client.post(f"{MEET}/sessions/{sid}/end")
        r = await client.post(f"{MEET}/sessions/{sid}/end?cancelled=true")
        assert r.status_code == 200
        assert r.json()["status"] == "ended"  # already ended — unchanged

    async def test_unknown_session_404(self, client):
        assert (await client.post(f"{MEET}/sessions/{uuid.uuid4()}/end")).status_code == 404


class TestProviders:
    async def test_lists_enabled_meeting_connectors(self, client, teams_connector, zoom_connector):
        r = await client.get(f"{MEET}/providers")
        assert r.status_code == 200
        body = r.json()
        assert body["driver"] == "off_platform"
        assert {p["provider"] for p in body["providers"]} == {"teams", "zoom"}

    async def test_anon_401(self, anon_client):
        assert (await anon_client.get(f"{MEET}/providers")).status_code == 401
