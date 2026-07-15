"""HxMeet P2 — embedded (LiveKit) case sessions.

Pins: per-tenant driver resolution (tenant.settings["meet"].driver beats the
platform default), fail-closed 501 when LiveKit isn't configured, server-side
token minting (room-scoped, identity-pinned, short-TTL — the API secret never
appears in a response), guest invites (single-use, expiring, pinned to a
portal-customer or email principal, uniform 404 anti-oracle on the public
exchange), the meet.join HxGuard registration under enforce mode, and the
signature-verified LiveKit webhook (presence stamps + idempotent auto-end).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import pytest_asyncio

from case_service.config import get_settings
from case_service.db.models import (
    CaseSessionParticipantModel,
    PortalCustomerModel,
    TenantModel,
)
from sqlalchemy import select, update
from tests.conftest import create_case, deploy_case_type

MEET = "/api/v1/meet"


@pytest_asyncio.fixture
async def livekit_cfg():
    """Embedded driver on + LiveKit 'configured' (restored by _isolate_settings)."""
    s = get_settings()
    s.meet_driver = "embedded"
    s.livekit_url = "wss://livekit.test"
    s.livekit_api_key = "lk_test_key"
    s.livekit_api_secret = "lk_test_secret_of_sufficient_length"
    yield s


async def _case(client) -> dict:
    ct = await deploy_case_type(client, name=f"Meet CT {uuid.uuid4().hex[:6]}")
    return await create_case(client, ct["id"])


async def _embedded_session(client) -> dict:
    case = await _case(client)
    r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={"title": "Video call"})
    assert r.status_code == 201, r.text
    return r.json()


def _sign_webhook(body: bytes) -> str:
    s = get_settings()
    return jwt.encode(
        {"iss": s.livekit_api_key,
         "sha256": base64.b64encode(hashlib.sha256(body).digest()).decode()},
        s.livekit_api_secret, algorithm="HS256",
    )


async def _post_webhook(client, event: dict, *, auth: str | None = None):
    body = json.dumps(event).encode()
    return await client.post(
        f"{MEET}/webhook/livekit", content=body,
        headers={"Authorization": auth if auth is not None else _sign_webhook(body),
                 "Content-Type": "application/json"},
    )


class TestStartEmbedded:
    async def test_start_creates_livekit_session(self, client, livekit_cfg):
        body = await _embedded_session(client)
        assert body["driver"] == "embedded"
        assert body["provider"] == "livekit"
        assert body["status"] == "active"
        assert body["join_url"] is None                      # joins mint tokens instead
        assert body["external_meeting_id"].startswith("vx-default-")
        assert body["external_meeting_id"].endswith(body["id"])

    async def test_unconfigured_embedded_501(self, client):
        get_settings().meet_driver = "embedded"              # no LiveKit keys set
        case = await _case(client)
        r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        assert r.status_code == 501

    async def test_tenant_driver_overrides_platform_default(self, client, session, livekit_cfg):
        livekit_cfg.meet_driver = "off_platform"             # platform default stays P1
        session.add(TenantModel(slug="default", name="Default",
                                settings={"meet": {"driver": "embedded"}}))
        await session.commit()
        body = await _embedded_session(client)
        assert body["driver"] == "embedded"

    async def test_providers_reports_resolved_driver(self, client, livekit_cfg):
        r = await client.get(f"{MEET}/providers")
        assert r.status_code == 200
        assert r.json()["driver"] == "embedded"
        assert r.json()["embedded_available"] is True


class TestWorkerToken:
    async def test_token_is_room_scoped_and_short_lived(self, client, livekit_cfg):
        sess = await _embedded_session(client)
        r = await client.post(f"{MEET}/sessions/{sess['id']}/token")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["url"] == "wss://livekit.test"
        assert body["room"] == sess["external_meeting_id"]
        assert body["identity"].startswith("user:")
        claims = jwt.decode(body["token"], livekit_cfg.livekit_api_secret,
                            algorithms=["HS256"], options={"verify_aud": False})
        assert claims["iss"] == "lk_test_key"
        assert claims["sub"] == body["identity"]
        assert claims["video"]["room"] == sess["external_meeting_id"]
        assert claims["video"]["roomJoin"] is True
        import time as _time
        assert claims["exp"] - _time.time() <= livekit_cfg.meet_token_ttl_seconds + 5

    async def test_secret_never_in_response(self, client, livekit_cfg):
        sess = await _embedded_session(client)
        r = await client.post(f"{MEET}/sessions/{sess['id']}/token")
        assert livekit_cfg.livekit_api_secret not in r.text

    async def test_token_on_off_platform_session_400(self, client, session, livekit_cfg):
        # Manufacture a P1-style session row directly.
        from case_service.db.models import CaseSessionModel
        case = await _case(client)
        row = CaseSessionModel(case_id=uuid.UUID(case["id"]), tenant_id="default",
                               driver="off_platform", provider="teams",
                               status="active", started_by="tester")
        session.add(row); await session.commit()
        r = await client.post(f"{MEET}/sessions/{row.id}/token")
        assert r.status_code == 400

    async def test_token_on_ended_session_409(self, client, livekit_cfg):
        sess = await _embedded_session(client)
        await client.post(f"{MEET}/sessions/{sess['id']}/end")
        r = await client.post(f"{MEET}/sessions/{sess['id']}/token")
        assert r.status_code == 409

    async def test_unknown_session_404(self, client, livekit_cfg):
        r = await client.post(f"{MEET}/sessions/{uuid.uuid4()}/token")
        assert r.status_code == 404

    async def test_meet_join_registered_with_hxguard(self, client, livekit_cfg):
        """Enforce mode: meet.join must be a registered case action (unknown =
        fail-closed 404 for everyone) — and an unrelated user gets the 404
        anti-oracle, not a 403."""
        from case_service.auth.jwt_handler import create_dev_token
        sess = await _embedded_session(client)
        s = get_settings()
        s.hxguard_case_enforcement = "enforce"
        r = await client.post(f"{MEET}/sessions/{sess['id']}/token")
        assert r.status_code == 200                          # admin bypass = registered

        outsider = create_dev_token(
            user_id=str(uuid.uuid4()), username="outsider", roles=["viewer"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
        )
        r2 = await client.post(f"{MEET}/sessions/{sess['id']}/token",
                               headers={"Authorization": f"Bearer {outsider}"})
        assert r2.status_code == 404


class TestGuestInvites:
    async def test_email_invite_returns_single_use_token(self, client, session, livekit_cfg):
        sess = await _embedded_session(client)
        r = await client.post(f"{MEET}/sessions/{sess['id']}/invites",
                              json={"email": "Guest@Example.com"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["identity"] == "email:guest@example.com"
        assert body["invite_token"] in body["join_path"]
        # Only the hash is stored, never the raw token.
        row = (await session.execute(
            select(CaseSessionParticipantModel)
            .where(CaseSessionParticipantModel.id == uuid.UUID(body["participant_id"]))
        )).scalars().one()
        assert row.invite_token_hash == hashlib.sha256(body["invite_token"].encode()).hexdigest()
        assert body["invite_token"] not in (row.invite_token_hash or "")

    async def test_customer_invite_pins_customer_identity(self, client, session, livekit_cfg):
        t = TenantModel(slug="default", name="Default", settings={})
        session.add(t); await session.flush()
        cust = PortalCustomerModel(tenant_id=t.id, primary_email="c@example.com",
                                   display_name="Cust Omer")
        session.add(cust); await session.commit()
        sess = await _embedded_session(client)
        r = await client.post(f"{MEET}/sessions/{sess['id']}/invites",
                              json={"customer_id": str(cust.id)})
        assert r.status_code == 201, r.text
        assert r.json()["identity"] == f"customer:{cust.id}"

    async def test_cross_tenant_customer_rejected(self, client, session, livekit_cfg):
        other = TenantModel(slug="other", name="Other", settings={})
        session.add(other); await session.flush()
        cust = PortalCustomerModel(tenant_id=other.id, primary_email="x@example.com",
                                   display_name="Not Yours")
        session.add(cust); await session.commit()
        sess = await _embedded_session(client)
        r = await client.post(f"{MEET}/sessions/{sess['id']}/invites",
                              json={"customer_id": str(cust.id)})
        assert r.status_code == 400

    async def test_invite_needs_a_principal(self, client, livekit_cfg):
        sess = await _embedded_session(client)
        r = await client.post(f"{MEET}/sessions/{sess['id']}/invites", json={})
        assert r.status_code == 400


class TestGuestExchange:
    async def _invite(self, client) -> tuple[dict, str]:
        sess = await _embedded_session(client)
        r = await client.post(f"{MEET}/sessions/{sess['id']}/invites",
                              json={"email": "guest@example.com"})
        return sess, r.json()["invite_token"]

    async def test_valid_invite_yields_pinned_token(self, client, anon_client, livekit_cfg):
        sess, token = await self._invite(client)
        r = await anon_client.post(f"{MEET}/guest/token", json={"invite_token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["identity"] == "email:guest@example.com"
        assert body["session_id"] == sess["id"]
        claims = jwt.decode(body["token"], livekit_cfg.livekit_api_secret,
                            algorithms=["HS256"], options={"verify_aud": False})
        assert claims["sub"] == "email:guest@example.com"
        assert claims["video"]["room"] == sess["external_meeting_id"]

    async def test_invite_is_single_use(self, client, anon_client, session, livekit_cfg):
        _, token = await self._invite(client)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        assert (await anon_client.post(f"{MEET}/guest/token",
                                       json={"invite_token": token})).status_code == 200
        # Same shared-StaticPool hazard as test_expired_invite_404: a late
        # request-teardown rollback can eat the committed consume — verify
        # token_used_at actually stuck before pinning the replay to 404.
        for _ in range(5):
            session.expire_all()
            used = (await session.execute(
                select(CaseSessionParticipantModel.token_used_at)
                .where(CaseSessionParticipantModel.invite_token_hash == token_hash)
            )).scalar_one()
            if used is not None:
                break
            r = await anon_client.post(f"{MEET}/guest/token", json={"invite_token": token})
            assert r.status_code in (200, 404)
        else:
            pytest.fail("could not persist the invite consume")
        assert (await anon_client.post(f"{MEET}/guest/token",
                                       json={"invite_token": token})).status_code == 404

    async def test_garbage_token_404(self, anon_client, livekit_cfg):
        r = await anon_client.post(f"{MEET}/guest/token", json={"invite_token": "nope"})
        assert r.status_code == 404

    async def test_expired_invite_404(self, client, anon_client, session, livekit_cfg):
        _, token = await self._invite(client)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        # Late request teardown from _invite can interleave a rollback on the
        # shared StaticPool connection and eat this write — verify it stuck.
        for _ in range(5):
            await session.execute(
                update(CaseSessionParticipantModel)
                .where(CaseSessionParticipantModel.invite_token_hash == token_hash)
                .values(invite_expires_at=past))
            await session.commit()
            session.expire_all()
            stored = (await session.execute(
                select(CaseSessionParticipantModel.invite_expires_at)
                .where(CaseSessionParticipantModel.invite_token_hash == token_hash)
            )).scalar_one()
            if stored is not None and stored.replace(tzinfo=timezone.utc) <= past:
                break
        else:
            pytest.fail("could not persist expired invite_expires_at")
        r = await anon_client.post(f"{MEET}/guest/token", json={"invite_token": token})
        assert r.status_code == 404

    async def test_invite_dies_with_the_session(self, client, anon_client, livekit_cfg):
        sess, token = await self._invite(client)
        await client.post(f"{MEET}/sessions/{sess['id']}/end")
        r = await anon_client.post(f"{MEET}/guest/token", json={"invite_token": token})
        assert r.status_code == 404


class TestWebhook:
    async def test_bad_signature_401(self, anon_client, livekit_cfg):
        r = await _post_webhook(anon_client, {"event": "room_finished"}, auth="garbage")
        assert r.status_code == 401

    async def test_unconfigured_rejects_all(self, anon_client):
        r = await _post_webhook(anon_client, {"event": "room_finished"}, auth="anything")
        assert r.status_code == 401

    async def test_participant_joined_and_left_stamped(self, client, anon_client, livekit_cfg):
        sess = await _embedded_session(client)
        room = sess["external_meeting_id"]
        await _post_webhook(anon_client, {"event": "participant_joined",
                                          "room": {"name": room},
                                          "participant": {"identity": "email:g@x.com"}})
        await _post_webhook(anon_client, {"event": "participant_left",
                                          "room": {"name": room},
                                          "participant": {"identity": "email:g@x.com"}})
        r = await client.get(f"{MEET}/sessions/{sess['id']}/participants")
        assert r.status_code == 200
        parts = {p["identity"]: p for p in r.json()["participants"]}
        assert parts["email:g@x.com"]["joined_at"]
        assert parts["email:g@x.com"]["left_at"]

    async def test_room_finished_auto_ends_idempotently(self, client, anon_client, livekit_cfg):
        sess = await _embedded_session(client)
        event = {"event": "room_finished", "room": {"name": sess["external_meeting_id"]}}
        assert (await _post_webhook(anon_client, event)).status_code == 200
        assert (await _post_webhook(anon_client, event)).status_code == 200  # idempotent
        r = await client.get(f"{MEET}/cases/{sess['case_id']}/sessions")
        assert r.json()["sessions"][0]["status"] == "ended"

    async def test_unknown_room_ignored(self, anon_client, livekit_cfg):
        r = await _post_webhook(anon_client, {"event": "room_finished",
                                              "room": {"name": f"vx-default-{uuid.uuid4()}"}})
        assert r.status_code == 200
