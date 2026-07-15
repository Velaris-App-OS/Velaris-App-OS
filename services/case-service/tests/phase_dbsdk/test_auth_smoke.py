"""DB SDK Phase 1b — real auth path smoke (the MySQL login proof).

auth_real.py had three raw-SQL sites that only break on a live non-Postgres
backend (they pass on SQLite, which tolerates the PG syntax loosely):

  1. _get_token_expiry_days   — SELECT ... WHERE key = ...  (`key` is reserved on MySQL)
  2. set_token_expiry         — INSERT ... ON CONFLICT       (PG-only upsert)
  3. refresh rotation         — UPDATE ... RETURNING         (MySQL 8 has no RETURNING)

All three are now ORM/Core constructs. This smoke drives the actual ASGI
endpoints — login -> refresh -> set-token-expiry — so the conversions are
exercised on whatever dialect the harness URL selects. On MySQL this is the
round-trip that clears the "real login on MySQL unverified" flag; on SQLite it
guards against a regression that would reintroduce raw SQL.
"""
from __future__ import annotations

import uuid

import pytest

from case_service.api.routers.auth_real import _hash_password
from case_service.db.models import HelixUserModel
from tests.conftest import ADMIN_HEADERS

_PASSWORD = "Sm0ke-Test-Pw!"


@pytest.mark.asyncio
async def test_login_refresh_set_expiry_roundtrip(client, anon_client, session):
    # Seed a real user with a bcrypt password (login_real verifies the hash).
    username = f"smoke_{uuid.uuid4().hex[:8]}"
    session.add(HelixUserModel(
        username=username,
        email=f"{username}@example.test",
        password_hash=_hash_password(_PASSWORD),
        roles=["admin"],
    ))
    await session.commit()

    # 1. Real login — exercises _make_tokens -> _get_token_expiry_days (site 1)
    #    and the per-user expired-token prune (the func.now() DELETE), and
    #    persists a refresh token.
    login = await anon_client.post("/api/v1/auth/real/login",
                                   json={"username": username, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    body = login.json()
    refresh_token = body["refresh_token"]
    assert body["access_token"] and refresh_token

    # 2. Refresh rotation — exercises the atomic check-and-revoke (site 3).
    refresh = await anon_client.post("/api/v1/auth/real/refresh",
                                     json={"refresh_token": refresh_token})
    assert refresh.status_code == 200, refresh.text
    new_refresh = refresh.json()["refresh_token"]
    assert new_refresh and new_refresh != refresh_token

    # The old (rotated) token is now revoked — reusing it must fail.
    replay = await anon_client.post("/api/v1/auth/real/refresh",
                                    json={"refresh_token": refresh_token})
    assert replay.status_code == 401, replay.text

    # 3. Admin sets token expiry — exercises the get-or-create upsert (site 2),
    #    twice, to cover both the insert and the update branch.
    for days in (45, 90):
        resp = await client.put("/api/v1/auth/real/settings/token-expiry",
                                json={"token_expiry_days": days}, headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["token_expiry_days"] == days

    # Read-back confirms the upsert persisted (and exercises site 1's SELECT shape).
    got = await client.get("/api/v1/auth/real/settings/token-expiry", headers=ADMIN_HEADERS)
    assert got.status_code == 200, got.text
    assert got.json()["token_expiry_days"] == 90
