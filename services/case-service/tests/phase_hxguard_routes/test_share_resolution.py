"""Case sharing — share targets resolve to real users.

Pins the 2026-07-14 fix: the Sharing tab accepted any free-text user id and
wrote it verbatim into the HxGuard tuple. Tokens carry the user UUID as
`sub`, so a share written for an email/username/typo granted access to
nobody while reporting success. Shares now resolve id/username/email to the
canonical user UUID and reject unknown users with 400; the listing carries
username/display_name; unshare resolves the same way (with a raw-value
fallback for legacy tuples).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import create_case, deploy_case_type

pytestmark = pytest.mark.asyncio


async def _seed_user(session, *, display_name: str | None = None):
    from case_service.db.models import HelixUserModel
    username = f"share_{uuid.uuid4().hex[:8]}"
    row = HelixUserModel(
        username=username, email=f"{username}@test.local",
        display_name=display_name, roles=["case_worker"], is_active=True,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _case(client) -> dict:
    ct = await deploy_case_type(client, name=f"Share CT {uuid.uuid4().hex[:6]}")
    return await create_case(client, ct["id"])


class TestShareResolution:
    async def test_share_by_email_stores_user_uuid(self, client, session):
        target = await _seed_user(session)
        case = await _case(client)
        r = await client.post(f"/api/v1/cases/{case['id']}/shares",
                              json={"user_id": target.email.upper(), "relation": "viewer"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["user_id"] == str(target.id)
        assert body["username"] == target.username

    async def test_share_by_username_stores_user_uuid(self, client, session):
        target = await _seed_user(session)
        case = await _case(client)
        r = await client.post(f"/api/v1/cases/{case['id']}/shares",
                              json={"user_id": target.username, "relation": "editor"})
        assert r.status_code == 201, r.text
        assert r.json()["user_id"] == str(target.id)

    async def test_share_by_uuid_still_works(self, client, session):
        target = await _seed_user(session)
        case = await _case(client)
        r = await client.post(f"/api/v1/cases/{case['id']}/shares",
                              json={"user_id": str(target.id), "relation": "viewer"})
        assert r.status_code == 201, r.text
        assert r.json()["user_id"] == str(target.id)

    async def test_unknown_user_400_not_silent_success(self, client):
        case = await _case(client)
        r = await client.post(f"/api/v1/cases/{case['id']}/shares",
                              json={"user_id": "nobody@nowhere.example", "relation": "viewer"})
        assert r.status_code == 400
        assert "No user found" in r.json()["detail"]

    async def test_inactive_user_rejected(self, client, session):
        target = await _seed_user(session)
        target.is_active = False
        session.add(target); await session.commit()
        case = await _case(client)
        r = await client.post(f"/api/v1/cases/{case['id']}/shares",
                              json={"user_id": target.username, "relation": "viewer"})
        assert r.status_code == 400

    async def test_listing_carries_username(self, client, session):
        target = await _seed_user(session, display_name="Share Target")
        case = await _case(client)
        await client.post(f"/api/v1/cases/{case['id']}/shares",
                          json={"user_id": target.email, "relation": "viewer"})
        r = await client.get(f"/api/v1/cases/{case['id']}/shares")
        assert r.status_code == 200, r.text
        entry = next(s for s in r.json() if s["user_id"] == str(target.id))
        assert entry["username"] == target.username
        assert entry["display_name"] == "Share Target"

    async def test_unshare_by_email_removes_uuid_tuple(self, client, session):
        target = await _seed_user(session)
        case = await _case(client)
        await client.post(f"/api/v1/cases/{case['id']}/shares",
                          json={"user_id": str(target.id), "relation": "viewer"})
        r = await client.delete(
            f"/api/v1/cases/{case['id']}/shares",
            params={"user_id": target.email, "relation": "viewer"},
        )
        assert r.status_code == 204, r.text
        r = await client.get(f"/api/v1/cases/{case['id']}/shares")
        assert all(s["user_id"] != str(target.id) for s in r.json())

    async def test_unshare_unknown_share_404(self, client, session):
        target = await _seed_user(session)
        case = await _case(client)
        r = await client.delete(
            f"/api/v1/cases/{case['id']}/shares",
            params={"user_id": target.email, "relation": "viewer"},
        )
        assert r.status_code == 404
