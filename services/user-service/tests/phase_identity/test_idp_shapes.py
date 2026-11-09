"""The two products' real document shapes, end to end.

Every other SAML test in this suite uses one fixture shape, which proves the
checks but not that we can read what Entra and Okta actually send. These do the
second thing: the documents in `_idp_captures` reproduce their namespace style,
claim URIs, value typing, signature placement and group encoding, and the tests
run them through the same verifier and the same ACS a real login uses.

What is still not proven here — and cannot be without a tenant — is their
signing keys and canonicalisation. That is signxml's half; ours is everything
below.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from velaris_shared.db.models import (
    AccessGroupModel,
    HelixUserModel,
    IdentityGroupMappingModel,
    OperatorAccessGroupModel,
    PortalModel,
    SsoProviderModel,
    TenantModel,
)
from user_service.identity import saml
from velaris_shared.tenancy import identity as tenant_identity
from tests.phase_identity import _idp_captures as caps
from tests.phase_identity import _saml_fixtures as fx

TENANT_SLUG = fx.TENANT_SLUG


def _cfg(**overrides) -> saml.SamlConfig:
    base = dict(
        idp_entity_id=caps.ENTRA_ISSUER,
        idp_sso_url="https://login.microsoftonline.com/x/saml2",
        idp_x509_certs=[fx.cert_body()],
        sp_entity_id=caps.ENTRA_AUDIENCE,
        acs_url=fx.ACS_URL,
        email_domain_allowlist=["acme.com"],
    )
    base.update(overrides)
    return saml.SamlConfig(**base)


class TestEntraShape:
    """Entra puts the assertion in a **default namespace** and signs it."""

    def test_a_default_namespace_assertion_is_read_correctly(self):
        result = saml.verify_response(
            caps.entra_signed(), cfg=_cfg(), acs_url=fx.ACS_URL)
        assert result.name_id.startswith("IPTOxLNTz")
        assert result.email == "ada@acme.com"
        assert result.display_name == "Ada Lovelace"
        assert result.session_index == "_entra-assertion-1"

    def test_groups_arrive_as_object_ids(self):
        """Entra asserts directory **GUIDs**, not group names.

        This is the whole reason `identity_group_mappings` exists: a deployment
        that expected names would map nothing, and every federated user would
        sign in with no access group at all.
        """
        result = saml.verify_response(
            caps.entra_signed(), cfg=_cfg(), acs_url=fx.ACS_URL)
        assert result.groups == list(caps.ENTRA_GROUP_IDS)
        assert all("-" in g and " " not in g for g in result.groups)

    def test_the_audience_is_the_application_id_uri(self):
        """Entra's audience is `api://…`, not our metadata entity id — so a
        provider configured with the wrong one must fail closed."""
        with pytest.raises(saml.SamlError) as caught:
            saml.verify_response(
                caps.entra_signed(), cfg=_cfg(sp_entity_id="https://sp.test/entity"),
                acs_url=fx.ACS_URL)
        assert caught.value.reason == "audience_mismatch"

    def test_the_issuer_trailing_slash_is_part_of_it(self):
        with pytest.raises(saml.SamlError) as caught:
            saml.verify_response(
                caps.entra_signed(),
                cfg=_cfg(idp_entity_id=caps.ENTRA_ISSUER.rstrip("/")),
                acs_url=fx.ACS_URL)
        assert caught.value.reason == "issuer_mismatch"

    def test_wrapping_still_fails_on_an_entra_shaped_document(self):
        """The XSW defence must not depend on the fixture's namespace style."""
        import base64

        from lxml import etree

        root = etree.fromstring(base64.b64decode(caps.entra_signed()))
        ns = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
        # The attacker's own assertion is unsigned — that is the attack: keep the
        # signed one in the document and hope the consumer reads the other.
        forged = etree.fromstring(caps.entra_response_xml(
            assertion_id="_forged", email="attacker@acme.com",
            name_id="attacker-1").encode()).find("./saml:Assertion", ns)
        root.insert(0, forged)
        result = saml.verify_response(
            fx.encode(etree.tostring(root)), cfg=_cfg(), acs_url=fx.ACS_URL)
        # The signature covers the real assertion, and nothing else is reachable.
        assert result.email == "ada@acme.com"


class TestOktaShape:
    """Okta prefixes everything `saml2:`, types its values, signs the Response."""

    def _okta_cfg(self, **overrides) -> saml.SamlConfig:
        base = dict(idp_entity_id=caps.OKTA_ISSUER, sp_entity_id=caps.OKTA_AUDIENCE)
        base.update(overrides)
        return _cfg(**base)

    def test_a_prefixed_typed_document_is_read_correctly(self):
        result = saml.verify_response(
            caps.okta_signed(), cfg=self._okta_cfg(), acs_url=fx.ACS_URL)
        assert result.email == "ada@acme.com"
        assert result.groups == ["Claims-Team", "Everyone"]
        assert result.session_index == "id-okta-session-1"

    def test_the_name_halves_become_a_display_name(self):
        """Okta's default app sends `firstName`/`lastName` and no display name.

        Without composing one, every Okta-provisioned user arrives named after
        their username.
        """
        result = saml.verify_response(
            caps.okta_signed(), cfg=self._okta_cfg(), acs_url=fx.ACS_URL)
        assert result.display_name == "Ada Lovelace"

    def test_an_email_format_name_id_is_still_the_address(self):
        """Okta's NameID is the address; a deployment that maps no email claim
        must still end up with one."""
        result = saml.verify_response(
            caps.okta_signed(email="", name_id="ada@acme.com"),
            cfg=self._okta_cfg(), acs_url=fx.ACS_URL)
        assert result.email == "ada@acme.com"


class TestThroughTheAcs:
    """The shapes, through the endpoint a browser actually posts to."""

    @staticmethod
    async def _provider(session, **config_overrides) -> SsoProviderModel:
        session.add(TenantModel(slug=TENANT_SLUG, name="Acme"))
        config = {
            "idp_entity_id": caps.ENTRA_ISSUER,
            "idp_sso_url": "https://login.microsoftonline.com/x/saml2",
            "idp_x509_certs": [fx.cert_body()],
            "sp_entity_id": caps.ENTRA_AUDIENCE,
            "acs_url": fx.ACS_URL,
            "email_domain_allowlist": ["acme.com"],
            "allowed_relay_state_prefixes": ["/studio"],
        }
        config.update(config_overrides)
        row = SsoProviderModel(tenant_id=TENANT_SLUG, provider="saml",
                               client_id="unused", enabled=True, config=config)
        session.add(row)
        await session.flush()

        # Both captures answer an SP-initiated login, so they quote the request
        # they were minted for. `/login` records that id; these fixtures post
        # straight to the ACS, so the row has to be seeded or `InResponseTo`
        # correctly refuses an assertion answering a request we never sent.
        from datetime import datetime, timedelta, timezone

        from velaris_shared.db.models import SamlAuthnRequestModel

        for request_id in ("_authnrequest-1", "id-authnrequest-1"):
            session.add(SamlAuthnRequestModel(
                request_id=request_id, provider_id=row.id,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)))
        await session.commit()
        tenant_identity.clear_cache()
        return row

    @pytest.mark.asyncio
    async def test_an_entra_assertion_provisions_and_signs_in(
        self, session, anon_client,
    ):
        await self._provider(session)
        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": caps.entra_signed()}, follow_redirects=False)
        assert resp.status_code == 303, resp.text

        user = (await session.execute(select(HelixUserModel).where(
            HelixUserModel.email == "ada@acme.com"))).scalars().first()
        assert user is not None and user.display_name == "Ada Lovelace"

    @pytest.mark.asyncio
    async def test_an_entra_group_guid_grants_its_mapped_access_group(
        self, session, anon_client,
    ):
        """The mapping is written against the GUID, because that is what arrives."""
        row = await self._provider(session)
        tenant = (await session.execute(select(TenantModel).where(
            TenantModel.slug == TENANT_SLUG))).scalar_one()
        portal = PortalModel(name=f"P-{uuid.uuid4().hex[:6]}", portal_type="staff",
                             modules=[], homepage="/", tenant_id=TENANT_SLUG)
        session.add(portal)
        await session.flush()
        group = AccessGroupModel(name=f"G-{uuid.uuid4().hex[:6]}", portal_id=portal.id,
                                 role_ids=[], is_active=True, tenant_id=TENANT_SLUG)
        session.add(group)
        await session.flush()
        session.add(IdentityGroupMappingModel(
            tenant_id=tenant.id, source="saml_jit",
            external_group=caps.ENTRA_GROUP_IDS[0],
            access_group_id=group.id, priority=100, created_by="tester"))
        await session.commit()

        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": caps.entra_signed()}, follow_redirects=False)
        assert resp.status_code == 303, resp.text

        user = (await session.execute(select(HelixUserModel).where(
            HelixUserModel.email == "ada@acme.com"))).scalars().first()
        granted = (await session.execute(select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == user.username))).scalars().all()
        assert [g.access_group_id for g in granted] == [group.id]
        assert row.id is not None

    @pytest.mark.asyncio
    async def test_an_okta_assertion_provisions_and_signs_in(
        self, session, anon_client,
    ):
        await self._provider(
            session, idp_entity_id=caps.OKTA_ISSUER, sp_entity_id=caps.OKTA_AUDIENCE)
        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": caps.okta_signed()}, follow_redirects=False)
        assert resp.status_code == 303, resp.text

        user = (await session.execute(select(HelixUserModel).where(
            HelixUserModel.email == "ada@acme.com"))).scalars().first()
        assert user is not None
        assert user.external_id == "ada@acme.com"    # Okta's NameID is the address
        assert user.display_name == "Ada Lovelace"
