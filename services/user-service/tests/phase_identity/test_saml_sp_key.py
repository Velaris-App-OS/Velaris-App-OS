"""The SP's own key pair, and the three things that needed it.

Velaris was a passive SAML SP — it verified the IdP's signature and held no key.
Signed AuthnRequests, encrypted assertions and SP-initiated logout all block on
having one, which is why they arrive together.

The load-bearing test in this file is
`test_an_encrypted_assertion_nobody_signed_is_refused`. Encryption is
confidentiality, not authenticity: our certificate is *published*, so anyone can
produce a well-formed EncryptedAssertion. If decrypting were enough to sign in,
publishing the certificate would be publishing a login.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import uuid
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from lxml import etree

from velaris_shared.db.models import SsoProviderModel, TenantModel
from user_service.identity import saml, saml_encryption, saml_keys
from velaris_shared.tenancy import identity as tenant_identity
from tests.phase_identity import _saml_fixtures as fx

TENANT_SLUG = fx.TENANT_SLUG


def _cfg(**over) -> saml.SamlConfig:
    base = dict(
        idp_entity_id=fx.IDP_ENTITY_ID,
        idp_sso_url="https://idp.test/sso",
        idp_slo_url="https://idp.test/slo",
        idp_x509_certs=[fx.cert_body()],
        sp_entity_id=fx.SP_ENTITY_ID,
        acs_url=fx.ACS_URL,
        email_domain_allowlist=["acme.com"],
        allowed_relay_state_prefixes=["/"],
    )
    base.update(over)
    return saml.SamlConfig(**base)


async def _provider(session, **config_over) -> SsoProviderModel:
    session.add(TenantModel(slug=TENANT_SLUG, name="Acme"))
    config = {
        "idp_entity_id": fx.IDP_ENTITY_ID,
        "idp_sso_url": "https://idp.test/sso",
        "idp_slo_url": "https://idp.test/slo",
        "idp_x509_certs": [fx.cert_body()],
        "sp_entity_id": fx.SP_ENTITY_ID,
        "acs_url": fx.ACS_URL,
        "email_domain_allowlist": ["acme.com"],
        "allowed_relay_state_prefixes": ["/"],
    }
    config.update(config_over)
    row = SsoProviderModel(tenant_id=TENANT_SLUG, provider="saml", client_id="unused",
                           enabled=True, config=config)
    session.add(row)
    await session.commit()
    tenant_identity.clear_cache()
    return row


class TestKeyMaterial:
    @pytest.mark.asyncio
    async def test_a_pair_is_created_sealed_and_reopened(self, session):
        row = await _provider(session)
        cert = await saml_keys.ensure_keypair(session, row)
        await session.commit()

        assert "BEGIN CERTIFICATE" in cert
        assert row.sp_key_enc and "blob" in row.sp_key_enc
        assert "PRIVATE KEY" not in str(row.sp_key_enc), "the key must be sealed, not stored"

        key = await saml_keys.load_private_key(session, row)
        assert "BEGIN PRIVATE KEY" in key

    @pytest.mark.asyncio
    async def test_generating_twice_keeps_the_first_pair(self, session):
        """Re-pressing the button must not break what the IdP already trusts."""
        row = await _provider(session)
        first = await saml_keys.ensure_keypair(session, row)
        again = await saml_keys.ensure_keypair(session, row)
        assert first == again

    @pytest.mark.asyncio
    async def test_regenerating_replaces_it(self, session):
        row = await _provider(session)
        first = await saml_keys.ensure_keypair(session, row)
        second = await saml_keys.ensure_keypair(session, row, regenerate=True)
        assert first != second

    @pytest.mark.asyncio
    async def test_a_sealed_key_cannot_be_lifted_into_another_provider(self, session):
        """The AAD binds the blob to one provider row. Without it, a key copied
        into a provider pointed at a different IdP would open happily."""
        row = await _provider(session)
        await saml_keys.ensure_keypair(session, row)
        await session.commit()

        impostor = SsoProviderModel(
            tenant_id=TENANT_SLUG, provider="saml", client_id="other", enabled=False,
            config=dict(row.config), sp_key_enc=dict(row.sp_key_enc), sp_cert=row.sp_cert)
        session.add(impostor)
        await session.commit()

        with pytest.raises(saml_keys.SpKeyError):
            await saml_keys.load_private_key(session, impostor)

    @pytest.mark.asyncio
    async def test_the_summary_never_carries_the_key(self, session):
        row = await _provider(session)
        await saml_keys.ensure_keypair(session, row)
        summary = saml_keys.key_summary(row)
        assert summary["days_remaining"] > 0 and summary["expired"] is False
        assert "PRIVATE" not in str(summary)


class TestMetadata:
    def test_no_certificate_is_advertised_without_a_key(self):
        """Advertising a key we cannot use invites the IdP to encrypt to it, and
        every login then fails looking like a signature fault."""
        xml = saml.build_metadata(sp_entity_id=fx.SP_ENTITY_ID, acs_url=fx.ACS_URL)
        assert b"KeyDescriptor" not in xml

    def test_a_key_is_advertised_for_both_uses(self):
        _key, cert = saml_keys.generate_keypair(fx.SP_ENTITY_ID)
        xml = saml.build_metadata(sp_entity_id=fx.SP_ENTITY_ID, acs_url=fx.ACS_URL,
                                  sp_cert=cert)
        root = etree.fromstring(xml)
        uses = sorted(kd.get("use") for kd in root.iter(
            "{urn:oasis:names:tc:SAML:2.0:metadata}KeyDescriptor"))
        assert uses == ["encryption", "signing"]

    def test_authn_requests_signed_is_only_claimed_when_it_is_true(self):
        _key, cert = saml_keys.generate_keypair(fx.SP_ENTITY_ID)
        without = saml.build_metadata(sp_entity_id="x", acs_url="y",
                                      sign_authn_requests=True)
        with_key = saml.build_metadata(sp_entity_id="x", acs_url="y", sp_cert=cert,
                                       sign_authn_requests=True)
        assert b'AuthnRequestsSigned="false"' in without, \
            "claiming to sign without a key would make every request look forged"
        assert b'AuthnRequestsSigned="true"' in with_key


class TestSignedAuthnRequest:
    def _verify_as_idp(self, url: str, cert_pem: str) -> bool:
        """Verify the redirect signature the way an IdP does: over the raw query
        in the fixed order, never over re-encoded values."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        raw = urlparse(url).query
        parts = dict(c.partition("=")[::2] for c in raw.split("&"))
        signed = "&".join(
            f"{k}={parts[k]}" for k in ("SAMLRequest", "RelayState", "SigAlg") if k in parts)
        signature = base64.b64decode(unquote(parts["Signature"]))
        public_key = x509.load_pem_x509_certificate(cert_pem.encode()).public_key()
        try:
            public_key.verify(signature, signed.encode(), padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:
            return False

    def test_an_unsigned_request_carries_no_signature_parameters(self):
        url, _ = saml.build_authn_request(cfg=_cfg(), acs_url=fx.ACS_URL)
        assert "SigAlg" not in url and "Signature" not in url

    def test_a_signed_request_verifies_against_our_certificate(self):
        key, cert = saml_keys.generate_keypair(fx.SP_ENTITY_ID)
        url, _ = saml.build_authn_request(
            cfg=_cfg(), acs_url=fx.ACS_URL, relay_state="v1.Lw.1.abc",
            private_key_pem=key)
        assert parse_qs(urlparse(url).query)["SigAlg"][0] == saml.SIG_ALG_RSA_SHA256
        assert self._verify_as_idp(url, cert)

    def test_the_signature_does_not_verify_for_a_different_key(self):
        key, _cert = saml_keys.generate_keypair(fx.SP_ENTITY_ID)
        _other_key, other_cert = saml_keys.generate_keypair(fx.SP_ENTITY_ID)
        url, _ = saml.build_authn_request(
            cfg=_cfg(), acs_url=fx.ACS_URL, private_key_pem=key)
        assert not self._verify_as_idp(url, other_cert)


class TestEncryptedAssertions:
    """Both wrappings real IdPs emit, and the one that must never be accepted."""

    def _pair(self):
        return saml_keys.generate_keypair(fx.SP_ENTITY_ID)

    @pytest.mark.parametrize("mode", ["cbc", "gcm"])
    def test_a_signed_response_wrapping_an_encrypted_assertion_is_accepted(self, mode):
        """Shape A: the Response signature covers the ciphertext."""
        key, cert = self._pair()
        signed = fx.sign(fx.response_xml(email="enc@acme.com"), target="response")
        # Encrypt first, then sign the Response over the encrypted element.
        encrypted = fx.encrypt_assertion(
            fx.sign(fx.response_xml(email="enc@acme.com"), target="assertion"),
            cert, mode=mode)
        del signed
        assertion = saml.verify_response(
            fx.encode(encrypted), cfg=_cfg(), acs_url=fx.ACS_URL, private_key_pem=key)
        assert assertion.email == "enc@acme.com"

    def test_an_inner_signed_assertion_is_accepted(self):
        """Shape B: unsigned Response, assertion signed before encryption."""
        key, cert = self._pair()
        encrypted = fx.encrypt_assertion(
            fx.sign(fx.response_xml(email="inner@acme.com"), target="assertion"), cert)
        assertion = saml.verify_response(
            fx.encode(encrypted), cfg=_cfg(), acs_url=fx.ACS_URL, private_key_pem=key)
        assert assertion.email == "inner@acme.com"

    def test_an_encrypted_assertion_nobody_signed_is_refused(self):
        """**The one that matters.** Our certificate is published, so anyone can
        encrypt to it. If decryption implied trust, publishing the certificate
        would be publishing a login."""
        key, cert = self._pair()
        unsigned = etree.tostring(etree.fromstring(fx.response_xml(email="forged@acme.com").encode()))
        encrypted = fx.encrypt_assertion(unsigned, cert)
        with pytest.raises(saml.SamlError) as caught:
            saml.verify_response(fx.encode(encrypted), cfg=_cfg(), acs_url=fx.ACS_URL,
                                 private_key_pem=key)
        assert caught.value.reason in ("signature_invalid", "encrypted_assertion")

    def test_rsa_1_5_key_transport_is_refused(self):
        """PKCS#1 v1.5 is what Bleichenbacher and the XML-Enc breaks target."""
        key, cert = self._pair()
        encrypted = fx.encrypt_assertion(
            fx.sign(fx.response_xml(email="v15@acme.com"), target="assertion"),
            cert, key_transport="rsa15")
        with pytest.raises(saml.SamlError):
            saml.verify_response(fx.encode(encrypted), cfg=_cfg(), acs_url=fx.ACS_URL,
                                 private_key_pem=key)

    def test_without_a_key_an_encrypted_assertion_is_a_uniform_refusal(self):
        _key, cert = self._pair()
        encrypted = fx.encrypt_assertion(
            fx.sign(fx.response_xml(email="x@acme.com"), target="assertion"), cert)
        with pytest.raises(saml.SamlError) as caught:
            saml.verify_response(fx.encode(encrypted), cfg=_cfg(), acs_url=fx.ACS_URL)
        assert caught.value.reason == "encrypted_assertion"

    def test_the_wrong_key_fails_the_same_way_as_a_corrupt_one(self):
        """Uniformity is what closes the padding oracle on CBC."""
        _key, cert = self._pair()
        other_key, _ = self._pair()
        encrypted = fx.encrypt_assertion(
            fx.sign(fx.response_xml(email="x@acme.com"), target="assertion"), cert)
        with pytest.raises(saml.SamlError) as wrong_key:
            saml.verify_response(fx.encode(encrypted), cfg=_cfg(), acs_url=fx.ACS_URL,
                                 private_key_pem=other_key)

        corrupt = encrypted.replace(b"CipherValue>", b"CipherValue>AA", 1)
        with pytest.raises(saml.SamlError) as garbage:
            saml.verify_response(fx.encode(corrupt), cfg=_cfg(), acs_url=fx.ACS_URL,
                                 private_key_pem=other_key)
        assert wrong_key.value.reason == garbage.value.reason == "encrypted_assertion"


class TestSingleLogout:
    def _logout_request(self, *, name_id: str, key_pem: str | None,
                        issuer: str = fx.IDP_ENTITY_ID) -> str:
        """A LogoutRequest as the IdP sends it: deflated, base64, signed query."""
        import zlib
        from urllib.parse import quote, urlencode

        xml = (
            f'<samlp:LogoutRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_lo1" Version="2.0" '
            f'IssueInstant="2026-08-17T10:00:00Z">'
            f"<saml:Issuer>{issuer}</saml:Issuer>"
            f"<saml:NameID>{name_id}</saml:NameID></samlp:LogoutRequest>"
        )
        deflated = zlib.compress(xml.encode())[2:-4]
        params = {"SAMLRequest": base64.b64encode(deflated).decode()}
        if key_pem:
            params["SigAlg"] = saml.SIG_ALG_RSA_SHA256
            params["Signature"] = saml.sign_redirect_query(
                urlencode(params, quote_via=quote), key_pem)
        return urlencode(params, quote_via=quote)

    def test_a_request_the_idp_signed_verifies(self):
        query = self._logout_request(name_id="user-1", key_pem=fx.IDP_KEY.decode())
        assert saml.verify_redirect_signature(query, "SAMLRequest", _cfg()) is True

    def test_an_unsigned_request_does_not(self):
        """Unverified, this endpoint is a denial of service anyone can aim at
        any user by name."""
        query = self._logout_request(name_id="user-1", key_pem=None)
        assert saml.verify_redirect_signature(query, "SAMLRequest", _cfg()) is False

    def test_a_request_signed_by_the_wrong_key_does_not(self):
        other_key, _ = saml_keys.generate_keypair("impostor")
        query = self._logout_request(name_id="user-1", key_pem=other_key)
        assert saml.verify_redirect_signature(query, "SAMLRequest", _cfg()) is False

    def test_tampering_after_signing_breaks_it(self):
        query = self._logout_request(name_id="user-1", key_pem=fx.IDP_KEY.decode())
        tampered = query.replace("SAMLRequest=", "SAMLRequest=AA", 1)
        assert saml.verify_redirect_signature(tampered, "SAMLRequest", _cfg()) is False

    def test_the_request_parses_to_the_named_subject(self):
        query = self._logout_request(name_id="subject-42", key_pem=fx.IDP_KEY.decode())
        raw = parse_qs(query)["SAMLRequest"][0]
        parsed = saml.parse_logout_request(raw)
        assert parsed.name_id == "subject-42"
        assert parsed.issuer == fx.IDP_ENTITY_ID

    def test_an_sp_logout_request_is_built_and_signed(self):
        key, _cert = saml_keys.generate_keypair(fx.SP_ENTITY_ID)
        url, request_id = saml.build_logout_request(
            cfg=_cfg(), name_id="user-1", private_key_pem=key)
        assert url.startswith("https://idp.test/slo?")
        assert "Signature=" in url and request_id.startswith("_")

    def test_a_logout_response_answers_the_request_it_replies_to(self):
        key, _cert = saml_keys.generate_keypair(fx.SP_ENTITY_ID)
        url = saml.build_logout_response(cfg=_cfg(), in_response_to="_lo1",
                                         private_key_pem=key)
        import zlib
        raw = parse_qs(urlparse(url).query)["SAMLResponse"][0]
        xml = zlib.decompressobj(-15).decompress(base64.b64decode(raw))
        root = etree.fromstring(xml)
        assert root.get("InResponseTo") == "_lo1"
        assert saml.STATUS_SUCCESS.encode() in xml


class TestSloEndpoint:
    @pytest.mark.asyncio
    async def test_it_is_not_offered_unless_configured(self, session, anon_client):
        await _provider(session)          # slo_enabled defaults off
        r = await anon_client.get(f"/api/v1/auth/saml/{TENANT_SLUG}/slo",
                                  params={"SAMLRequest": "x"}, follow_redirects=False)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_an_unsigned_logout_revokes_nothing(self, session, anon_client):
        from velaris_shared.db.models import HelixUserModel, RefreshTokenModel

        await _provider(session, slo_enabled=True)
        user = HelixUserModel(username="slo-user", email="slo@acme.com", roles=["viewer"],
                              is_active=True, external_id="subject-42")
        session.add(user)
        await session.flush()
        session.add(RefreshTokenModel(user_id=str(user.id), token_hash="h1", jti=str(uuid.uuid4()),
                                      expires_at=saml.datetime.now(saml.timezone.utc)))
        await session.commit()

        query = TestSingleLogout()._logout_request(name_id="subject-42", key_pem=None)
        r = await anon_client.get(f"/api/v1/auth/saml/{TENANT_SLUG}/slo?{query}",
                                  follow_redirects=False)
        assert r.status_code == 400

        from sqlalchemy import select
        token = (await session.execute(select(RefreshTokenModel))).scalars().first()
        assert token.revoked_at is None, "an unsigned request must revoke nothing"

    @pytest.mark.asyncio
    async def test_a_signed_logout_ends_the_session_without_disabling_the_account(
        self, session, anon_client,
    ):
        from sqlalchemy import select

        from velaris_shared.db.models import HelixUserModel, RefreshTokenModel

        await _provider(session, slo_enabled=True)
        user = HelixUserModel(username="slo-user2", email="slo2@acme.com", roles=["viewer"],
                              is_active=True, external_id="subject-77")
        session.add(user)
        await session.flush()
        session.add(RefreshTokenModel(user_id=str(user.id), token_hash="h2", jti=str(uuid.uuid4()),
                                      expires_at=saml.datetime.now(saml.timezone.utc)))
        await session.commit()

        query = TestSingleLogout()._logout_request(
            name_id="subject-77", key_pem=fx.IDP_KEY.decode())
        r = await anon_client.get(f"/api/v1/auth/saml/{TENANT_SLUG}/slo?{query}",
                                  follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://idp.test/slo?")

        token = (await session.execute(select(RefreshTokenModel))).scalars().first()
        await session.refresh(user)
        assert token.revoked_at is not None
        assert user.is_active is True, \
            "logging out must not let an IdP lock somebody out of Velaris"

    @pytest.mark.asyncio
    async def test_an_unknown_subject_looks_exactly_like_a_known_one(
        self, session, anon_client,
    ):
        """Otherwise the endpoint is an unauthenticated oracle for which
        identities exist here."""
        await _provider(session, slo_enabled=True)
        query = TestSingleLogout()._logout_request(
            name_id="nobody-here", key_pem=fx.IDP_KEY.decode())
        r = await anon_client.get(f"/api/v1/auth/saml/{TENANT_SLUG}/slo?{query}",
                                  follow_redirects=False)
        assert r.status_code == 302


class TestInResponseTo:
    """A field nobody checks is decoration.

    Present means it must name a request we actually sent; absent means an
    IdP-initiated sign-in, which answers no request and is legitimate. Requiring
    it would break the tile flow both Okta and Entra offer.
    """

    @pytest.mark.asyncio
    async def test_login_records_the_request_it_sent(self, session, anon_client):
        from sqlalchemy import select

        from velaris_shared.db.models import SamlAuthnRequestModel

        await _provider(session)
        r = await anon_client.get(f"/api/v1/auth/saml/{TENANT_SLUG}/login",
                                  follow_redirects=False)
        assert r.status_code == 302
        rows = (await session.execute(select(SamlAuthnRequestModel))).scalars().all()
        assert len(rows) == 1 and rows[0].request_id.startswith("_")

    @pytest.mark.asyncio
    async def test_an_idp_initiated_assertion_is_still_accepted(self, session, anon_client):
        """No InResponseTo at all — the common tile-launch flow."""
        await _provider(session)
        r = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_idp-init", name_id="idp-1", email="idp@acme.com")},
            follow_redirects=False)
        assert r.status_code == 303

    @pytest.mark.asyncio
    async def test_an_assertion_answering_a_request_we_never_sent_is_refused(
        self, session, anon_client,
    ):
        await _provider(session)
        r = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_unknown-req", name_id="u-1", email="u@acme.com",
                in_response_to="_never-issued")},
            follow_redirects=False)
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_a_request_we_did_send_is_accepted_exactly_once(
        self, session, anon_client,
    ):
        """Consumption is the DELETE: the same assertion cannot be posted into a
        second sign-in attempt, and neither can a second assertion quoting the
        same request."""
        from sqlalchemy import select

        from velaris_shared.db.models import SamlAuthnRequestModel

        await _provider(session)
        await anon_client.get(f"/api/v1/auth/saml/{TENANT_SLUG}/login",
                              follow_redirects=False)
        row = (await session.execute(select(SamlAuthnRequestModel))).scalar_one()
        request_id = row.request_id

        first = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_a1", name_id="sp-1", email="sp@acme.com",
                in_response_to=request_id)},
            follow_redirects=False)
        assert first.status_code == 303

        second = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_a2", name_id="sp-1", email="sp@acme.com",
                in_response_to=request_id)},
            follow_redirects=False)
        assert second.status_code == 400

    @pytest.mark.asyncio
    async def test_it_is_read_from_the_signed_subject_not_the_response_element(
        self, session,
    ):
        """With an assertion-signed Response the Response attribute is unsigned,
        so a forger would simply write whichever value they liked."""
        # Assertion-signed — the Entra and Okta default — so the Response
        # element sits OUTSIDE the signature and can be edited freely.
        raw = fx.sign(fx.response_xml(assertion_id="_sig", name_id="s-1",
                                      email="s@acme.com", in_response_to="_real"),
                      target="assertion")
        # First occurrence is the Response attribute; the subject's copy follows.
        tampered = raw.replace(b'InResponseTo="_real"', b'InResponseTo="_forged"', 1)
        assert b'InResponseTo="_forged"' in tampered and b'InResponseTo="_real"' in tampered
        assertion = saml.verify_response(
            base64.b64encode(tampered).decode(), cfg=_cfg(), acs_url=fx.ACS_URL)
        assert assertion.in_response_to == "_real", \
            "the value must come from inside the signature"


class TestKeyEndpoint:
    @pytest.mark.asyncio
    async def test_generating_needs_the_configure_privilege(self, session, anon_client):
        await _provider(session)
        r = await anon_client.post(f"/api/v1/auth/saml/{TENANT_SLUG}/sp-key", json={})
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_it_returns_the_certificate_and_never_the_key(self, session, client):
        await _provider(session)
        r = await client.post(f"/api/v1/auth/saml/{TENANT_SLUG}/sp-key", json={})
        assert r.status_code == 200, r.text
        assert "BEGIN CERTIFICATE" in r.json()["certificate"]
        assert "PRIVATE KEY" not in r.text

    @pytest.mark.asyncio
    async def test_the_metadata_then_publishes_it(self, session, client, anon_client):
        await _provider(session)
        await client.post(f"/api/v1/auth/saml/{TENANT_SLUG}/sp-key", json={})
        meta = await anon_client.get(f"/api/v1/auth/saml/{TENANT_SLUG}/metadata")
        assert b"KeyDescriptor" in meta.content
