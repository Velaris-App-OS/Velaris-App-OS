"""Portal customer tokens are never signed with a public key (2026-10-21).

`_customer_jwt_secret()` used `VELARIS_CASE_SECRET_KEY`, defaulting to the
string `dev-secret-change-me`, and no install path ever set that variable: a
token anyone could sign for a known customer id read that customer's account
(proven live on the dev box). Now an operator's own key keeps working and
otherwise the key is derived from the install's auth secret.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time

import jwt
import pytest
import pytest_asyncio

from portal_service.api.routers import portal_customers as pc
from velaris_shared.config import get_settings
from velaris_shared.db.models import PortalCustomerModel, TenantModel


@pytest.fixture(autouse=True)
def _accounts_on():
    from velaris_shared.core.feature_flags import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("customer_accounts")
    _ENABLED_VERSIONS["customer_accounts"] = "v1"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("customer_accounts", None)
    else:
        _ENABLED_VERSIONS["customer_accounts"] = prior


@pytest_asyncio.fixture
async def jane(session):
    t = TenantModel(slug="keyco", name="KeyCo", settings={"portal": {"enabled": True}})
    session.add(t); await session.flush()
    c = PortalCustomerModel(tenant_id=t.id, primary_email="jane@keyco.test", display_name="Jane",
                            verified=True)
    session.add(c); await session.commit()
    return c


def _claims(customer) -> dict:
    now = int(time.time())
    return {"sub": str(customer.id), "tenant_id": str(customer.tenant_id), "slug": "keyco",
            "type": "portal_customer", "iat": now, "exp": now + 600,
            "iss": "helix-portal", "aud": "helix-portal-api"}


async def _account(client, token):
    return await client.get("/api/v1/portal/keyco/account",
                            headers={"Authorization": f"Bearer {token}"})


async def test_a_token_signed_with_the_old_public_key_is_refused(client, jane, monkeypatch):
    monkeypatch.delenv("VELARIS_CASE_SECRET_KEY", raising=False)
    forged = jwt.encode(_claims(jane), "dev-secret-change-me", algorithm="HS256")
    assert (await _account(client, forged)).status_code == 401


async def test_the_old_public_key_is_refused_even_when_written_in(client, jane, monkeypatch):
    monkeypatch.setenv("VELARIS_CASE_SECRET_KEY", "dev-secret-change-me")
    forged = jwt.encode(_claims(jane), "dev-secret-change-me", algorithm="HS256")
    assert (await _account(client, forged)).status_code == 401


async def test_the_services_own_token_works(client, jane, monkeypatch):
    monkeypatch.delenv("VELARIS_CASE_SECRET_KEY", raising=False)
    token = pc._mint_customer_token(str(jane.id), str(jane.tenant_id), "keyco")
    r = await _account(client, token)
    assert r.status_code == 200, r.text
    assert r.json()["primary_email"] == "jane@keyco.test"


async def test_an_operators_own_key_keeps_working(client, jane, monkeypatch):
    monkeypatch.setenv("VELARIS_CASE_SECRET_KEY", "an-operator-chose-this-long-key-" + "x" * 16)
    mine = jwt.encode(_claims(jane), "an-operator-chose-this-long-key-" + "x" * 16, algorithm="HS256")
    assert (await _account(client, mine)).status_code == 200


def test_the_key_is_not_the_auth_secret_itself(monkeypatch):
    # A token signed with the staff key must never pass as a customer token.
    monkeypatch.delenv("VELARIS_CASE_SECRET_KEY", raising=False)
    key = pc._customer_jwt_secret()
    assert key != get_settings().auth_secret
    assert len(key) == 64


def test_rotating_the_auth_secret_changes_the_key(monkeypatch):
    monkeypatch.delenv("VELARIS_CASE_SECRET_KEY", raising=False)
    s = get_settings()
    before = pc._customer_jwt_secret()
    monkeypatch.setattr(s, "auth_secret", s.auth_secret + "-rotated")
    assert pc._customer_jwt_secret() != before


def test_no_public_fallback_left_in_the_source():
    import inspect
    src = inspect.getsource(pc._customer_jwt_secret)
    assert 'getenv("VELARIS_CASE_SECRET_KEY", "dev-secret-change-me")' not in src
