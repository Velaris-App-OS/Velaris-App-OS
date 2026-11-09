"""Phase 13 tests — Auth/SSO.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import pytest


class TestAuthModels:
    def test_authenticated_user_roles(self):
        """Rewritten by Phase 4 step 2. This asserted that an admin *is* every
        other role — `is_designer`, `is_case_worker`, and `has_role("anything")`
        all returned True. That was the shipped behaviour, and it is the defect
        the phase removes: no gate could distinguish "holds the designer role"
        from "is an admin". The flags are now literal and display-only."""
        from velaris_shared.auth.models import AuthenticatedUser
        admin = AuthenticatedUser(user_id="u1", roles=["admin"])
        assert admin.is_admin is True
        assert admin.is_designer is False
        assert admin.is_case_worker is False
        assert not hasattr(admin, "has_role")

    def test_designer_role(self):
        from velaris_shared.auth.models import AuthenticatedUser
        user = AuthenticatedUser(user_id="u2", roles=["designer"])
        assert user.is_admin is False
        assert user.is_designer is True
        assert user.is_case_worker is False

    def test_case_worker_role(self):
        from velaris_shared.auth.models import AuthenticatedUser
        user = AuthenticatedUser(user_id="u3", roles=["case_worker"])
        assert user.is_case_worker is True
        assert user.is_designer is False

    def test_to_dict(self):
        from velaris_shared.auth.models import AuthenticatedUser
        user = AuthenticatedUser(user_id="u1", username="test", email="t@e.com", roles=["admin"])
        d = user.to_dict()
        assert d["user_id"] == "u1"
        assert d["is_admin"] is True
        assert "username" in d


class TestJWTHandler:
    def test_create_dev_token(self):
        from velaris_shared.auth.jwt_handler import create_dev_token
        token = create_dev_token("test-user", roles=["admin"])
        assert isinstance(token, str)
        assert len(token) > 10

    def test_decode_dev_token(self):
        from velaris_shared.auth.jwt_handler import create_dev_token, decode_jwt_token
        token = create_dev_token("test-user", roles=["admin"], secret="test-secret")
        claims = decode_jwt_token(token, secret="test-secret")
        assert claims["sub"] == "test-user"

    def test_decode_invalid_token(self):
        from velaris_shared.auth.jwt_handler import decode_jwt_token
        try:
            decode_jwt_token("invalid-token", secret="test-secret")
            assert False, "Should have raised"
        except Exception:
            pass

    def test_extract_keycloak_claims(self):
        from velaris_shared.auth.jwt_handler import extract_user_from_claims
        claims = {
            "sub": "user-123",
            "preferred_username": "john",
            "email": "john@example.com",
            "realm_access": {"roles": ["admin", "case_worker"]},
            "groups": ["/engineering"],
        }
        info = extract_user_from_claims(claims)
        assert info["user_id"] == "user-123"
        assert "admin" in info["roles"]
        assert info["email"] == "john@example.com"

    def test_extract_simple_claims(self):
        from velaris_shared.auth.jwt_handler import extract_user_from_claims
        claims = {"sub": "u1", "roles": ["viewer"], "name": "Test"}
        info = extract_user_from_claims(claims)
        assert info["roles"] == ["viewer"]
        assert info["username"] == "Test"


async def _mk_login_user(session, username: str, roles: list[str], password: str = "S3cure-pass!") -> str:
    """Real-auth era: login validates against velaris_users with a bcrypt hash."""
    from velaris_shared.auth.passwords import hash_password as _hash_password
    from velaris_shared.db.models import HelixUserModel
    session.add(HelixUserModel(
        username=username, email=f"{username}@test.local",
        password_hash=_hash_password(password), roles=roles, is_active=True,
    ))
    await session.commit()
    return password


class TestAuthAPI:
    async def test_login_requires_password(self, client):
        # the passwordless dev login is gone — schema demands a password
        resp = await client.post("/api/v1/auth/login", json={"username": "admin"})
        assert resp.status_code == 422

    async def test_real_login(self, client, session):
        pw = await _mk_login_user(session, "p13-admin", ["admin"])
        resp = await client.post("/api/v1/auth/login",
                                 json={"username": "p13-admin", "password": pw})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "admin" in data["user"]["roles"]

    async def test_login_different_roles(self, client, session):
        pw = await _mk_login_user(session, "p13-designer", ["designer"])
        resp = await client.post("/api/v1/auth/login",
                                 json={"username": "p13-designer", "password": pw})
        assert resp.status_code == 200
        assert "designer" in resp.json()["user"]["roles"]
        assert "admin" not in resp.json()["user"]["roles"]

    async def test_login_wrong_password_401(self, client, session):
        await _mk_login_user(session, "p13-worker", ["case_worker"])
        resp = await client.post("/api/v1/auth/login",
                                 json={"username": "p13-worker", "password": "wrong"})
        assert resp.status_code == 401

    async def test_get_me_dev_mode(self, client):
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert "user_id" in data
        assert "roles" in data

    async def test_list_roles(self, client):
        resp = await client.get("/api/v1/auth/roles")
        assert resp.status_code == 200
        roles = resp.json()["roles"]
        assert len(roles) >= 5
        role_ids = [r["id"] for r in roles]
        assert "admin" in role_ids
        assert "designer" in role_ids
        assert "case_worker" in role_ids

    async def test_login_returns_valid_token(self, client, session):
        pw = await _mk_login_user(session, "p13-token-user", ["admin"])
        login = await client.post("/api/v1/auth/login",
                                  json={"username": "p13-token-user", "password": pw})
        token = login.json()["access_token"]

        # Use token to call /me
        resp = await client.get("/api/v1/auth/me", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200


class TestRoleBasedAccess:
    """Rewritten by Phase 4 step 2. These asserted `has_role()`, which was
    deleted: it answered True for *every* role once the caller was admin, so it
    could not tell "holds the designer role" from "is an admin" — the defect the
    phase exists to remove. The flags that remain are display-only."""

    def test_role_flags_are_literal_not_inherited(self):
        from velaris_shared.auth.models import AuthenticatedUser
        user = AuthenticatedUser(user_id="u1", roles=["admin"])
        assert user.is_admin is True
        assert user.is_designer is False, "admin must not imply designer any more"

    def test_flags_are_false_for_an_unprivileged_user(self):
        from velaris_shared.auth.models import AuthenticatedUser
        user = AuthenticatedUser(user_id="u1", roles=["viewer"])
        assert user.is_admin is False
        assert user.is_designer is False

    def test_has_role_is_gone(self):
        from velaris_shared.auth.models import AuthenticatedUser
        assert not hasattr(AuthenticatedUser(user_id="u1", roles=[]), "has_role")
