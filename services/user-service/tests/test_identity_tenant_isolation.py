"""One tenant's admin cannot touch another tenant's identity configuration (R5e step 10).

Found when the split put user-service's routes in front of the other-apps probe
(it also tries id-keyed writes), 2026-10-21. Both holes predate the split, and
neither route had a path-guard rule, so the original probe never mapped them:

* `DELETE /api/v1/scim/tokens/{token_id}` — evil's admin revoked acme's SCIM
  token, and acme's IdP started getting 401s;
* `POST /api/v1/auth/saml/{tenant}/sp-key` — the provider is found by the tenant
  in the URL, so evil's admin could regenerate acme's SAML SP key (acme's SSO
  breaks until its IdP re-imports the metadata). `/{tenant}/test` likewise ran
  acme's provisioning decision for evil.

Each is tried by the least-privileged user who should be refused (another
tenant's admin) and by the one who should succeed (acme's own admin).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.db.models import (
    ScimTokenModel,
    SsoProviderModel,
    TenantModel,
    UserDirectoryModel,
)
from velaris_shared.tenancy import identity as tenant_identity
from tests.phase_identity import _saml_fixtures as fx

ACME, EVIL = "saml-acme", "evil-co"


def _h(user_id: str) -> dict[str, str]:
    s = get_settings()
    token = create_dev_token(user_id=user_id, username=user_id, roles=["admin"],
                             secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def world(session):
    session.add_all([
        TenantModel(id=uuid.uuid4(), slug=ACME, name="Acme"),
        TenantModel(id=uuid.uuid4(), slug=EVIL, name="Evil"),
        UserDirectoryModel(id=uuid.uuid4(), user_id="acme-admin", tenant_id=ACME),
        UserDirectoryModel(id=uuid.uuid4(), user_id="mallory", tenant_id=EVIL),
    ])
    token = ScimTokenModel(tenant_id=ACME, name="acme idp", token_hash="0" * 64,
                           scopes=[], created_by="acme-admin")
    provider = SsoProviderModel(tenant_id=ACME, provider="saml", client_id="unused", enabled=True,
                                config={"idp_entity_id": fx.IDP_ENTITY_ID,
                                        "idp_sso_url": "https://idp.test/sso",
                                        "idp_x509_certs": [fx.cert_body()],
                                        "sp_entity_id": fx.SP_ENTITY_ID, "acs_url": fx.ACS_URL,
                                        "email_domain_allowlist": ["acme.com"],
                                        "allowed_relay_state_prefixes": ["/"]})
    session.add_all([token, provider])
    await session.commit()
    tenant_identity.clear_cache()
    return {"token": token.id, "provider": provider.id}


async def _row(session, model, id_):
    session.expire_all()
    return (await session.execute(select(model).where(model.id == id_))).scalar_one()


async def test_another_tenants_admin_cannot_revoke_a_scim_token(client, session, world):
    r = await client.delete(f"/api/v1/scim/tokens/{world['token']}", headers=_h("mallory"))
    assert r.status_code == 404
    assert (await _row(session, ScimTokenModel, world["token"])).revoked_at is None


async def test_the_tenants_own_admin_can(client, session, world):
    r = await client.delete(f"/api/v1/scim/tokens/{world['token']}", headers=_h("acme-admin"))
    assert r.status_code == 204
    assert (await _row(session, ScimTokenModel, world["token"])).revoked_at is not None


async def test_another_tenants_admin_cannot_make_or_replace_the_saml_key(client, session, world):
    r = await client.post(f"/api/v1/auth/saml/{ACME}/sp-key", json={"regenerate": True},
                          headers=_h("mallory"))
    assert r.status_code == 404
    assert (await _row(session, SsoProviderModel, world["provider"])).sp_key_enc is None


async def test_another_tenants_admin_cannot_run_the_saml_tester(client, world):
    r = await client.post(f"/api/v1/auth/saml/{ACME}/test", json={"saml_response": "PHg+PC94Pg=="},
                          headers=_h("mallory"))
    assert r.status_code == 404


async def test_the_tenants_own_admin_makes_the_saml_key(client, session, world):
    r = await client.post(f"/api/v1/auth/saml/{ACME}/sp-key", json={}, headers=_h("acme-admin"))
    assert r.status_code == 200, r.text
    assert (await _row(session, SsoProviderModel, world["provider"])).sp_key_enc


async def test_the_public_saml_routes_still_answer_anonymously(anon_client, world):
    """The guard leaves a request with no staff identity alone: an IdP and a
    signing-in browser carry none."""
    r = await anon_client.get(f"/api/v1/auth/saml/{ACME}/metadata")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("url_tenant", ["default", "DEFAULT", "nonexistent-co"])
async def test_nor_the_installs_own_provider_through_another_name(client, session, url_tenant):
    """A tenant-less SAML row serves whatever tenant token the URL carries
    (single-tenant installs). Neither `default` nor an unknown name may let
    another tenant's admin regenerate the install's key."""
    session.add_all([
        TenantModel(id=uuid.uuid4(), slug=EVIL, name="Evil"),
        UserDirectoryModel(id=uuid.uuid4(), user_id="mallory", tenant_id=EVIL),
    ])
    row = SsoProviderModel(tenant_id=None, provider="saml", client_id="unused", enabled=True,
                           config={"idp_entity_id": fx.IDP_ENTITY_ID,
                                   "idp_sso_url": "https://idp.test/sso",
                                   "idp_x509_certs": [fx.cert_body()],
                                   "sp_entity_id": fx.SP_ENTITY_ID, "acs_url": fx.ACS_URL,
                                   "email_domain_allowlist": ["acme.com"],
                                   "allowed_relay_state_prefixes": ["/"]})
    session.add(row)
    await session.commit()
    tenant_identity.clear_cache()
    r = await client.post(f"/api/v1/auth/saml/{url_tenant}/sp-key", json={"regenerate": True},
                          headers=_h("mallory"))
    assert r.status_code == 404, (url_tenant, r.status_code, r.text[:120])
    assert (await _row(session, SsoProviderModel, row.id)).sp_key_enc is None


# ── platform-shared rows (no tenant): readable by all, changed by the operator ──
# Found on a clean box, 2026-10-22: a row the install's operator creates without a
# tenant is platform-wide, and the path guard skipped it — so any tenant's admin
# rewrote the operator's SSO provider (its endpoints too: whoever signs in with
# it then signs in through the attacker's server) and revoked its SCIM token.

@pytest.fixture
async def shared(session):
    session.add_all([
        TenantModel(id=uuid.uuid4(), slug=EVIL, name="Evil"),
        UserDirectoryModel(id=uuid.uuid4(), user_id="mallory", tenant_id=EVIL),
        UserDirectoryModel(id=uuid.uuid4(), user_id="operator", tenant_id="default"),
    ])
    if (await session.execute(select(TenantModel).where(TenantModel.slug == "default"))).first() is None:
        session.add(TenantModel(id=uuid.uuid4(), slug="default", name="Default"))
    token = ScimTokenModel(tenant_id=None, name="platform idp", token_hash="1" * 64,
                           scopes=[], created_by="operator")
    provider = SsoProviderModel(tenant_id=None, provider="google", client_id="platform-client",
                                enabled=True, config={})
    session.add_all([token, provider])
    await session.commit()
    tenant_identity.clear_cache()
    return {"token": token.id, "provider": provider.id}


async def test_another_tenants_admin_cannot_change_a_platform_wide_sso_provider(client, session, shared):
    pid = shared["provider"]
    r = await client.patch(f"/api/v1/auth/real/sso/providers/{pid}", headers=_h("mallory"),
                           json={"client_id": "evil", "client_secret": "evil-secret"})
    assert r.status_code == 404
    r = await client.delete(f"/api/v1/auth/real/sso/providers/{pid}", headers=_h("mallory"))
    assert r.status_code == 404
    row = await _row(session, SsoProviderModel, pid)
    assert row.client_id == "platform-client"


async def test_another_tenants_admin_cannot_revoke_a_platform_wide_scim_token(client, session, shared):
    r = await client.delete(f"/api/v1/scim/tokens/{shared['token']}", headers=_h("mallory"))
    assert r.status_code == 404
    assert (await _row(session, ScimTokenModel, shared["token"])).revoked_at is None


async def test_the_operator_still_changes_them(client, session, shared):
    r = await client.patch(f"/api/v1/auth/real/sso/providers/{shared['provider']}",
                           headers=_h("operator"), json={"client_id": "rotated"})
    assert r.status_code == 200, r.text
    assert (await _row(session, SsoProviderModel, shared["provider"])).client_id == "rotated"
    r = await client.delete(f"/api/v1/scim/tokens/{shared['token']}", headers=_h("operator"))
    assert r.status_code == 204
    assert (await _row(session, ScimTokenModel, shared["token"])).revoked_at is not None


async def test_every_tenant_still_reads_them(client, shared):
    """Reading is unchanged: a platform-wide provider is every tenant's sign-in."""
    r = await client.get("/api/v1/auth/real/sso/providers/manage", headers=_h("mallory"))
    assert r.status_code == 200
    assert str(shared["provider"]) in r.text
