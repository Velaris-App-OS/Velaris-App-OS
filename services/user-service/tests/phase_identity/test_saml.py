"""SAML 2.0 SP — the assertion is a bearer credential, so every check is a test.

An ACS endpoint accepts bytes from anybody. Everything that makes it safe is a
refusal, and a refusal that quietly stops happening looks exactly like a working
login. The negative cases below are therefore the point of this file; the one
positive case exists so the negatives cannot pass vacuously.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import uuid
from urllib.parse import unquote
from datetime import datetime, timedelta, timezone

import pytest
from lxml import etree
from sqlalchemy import select

from velaris_shared.db.models import (
    AuthOtpModel,
    HelixUserModel,
    IdentityProvisionLogModel,
    SamlAssertionReplayModel,
    SsoProviderModel,
    TenantModel,
)
from user_service.identity import saml
from velaris_shared.tenancy import identity as tenant_identity
from tests.phase_identity import _saml_fixtures as fx


def _cfg(**overrides) -> saml.SamlConfig:
    base = dict(
        idp_entity_id=fx.IDP_ENTITY_ID,
        idp_sso_url="https://idp.test/sso",
        idp_x509_certs=[fx.cert_body()],
        sp_entity_id=fx.SP_ENTITY_ID,
        acs_url=fx.ACS_URL,
        email_domain_allowlist=["acme.com"],
        allowed_relay_state_prefixes=["/studio"],
    )
    base.update(overrides)
    return saml.SamlConfig(**base)


def _verify(response: str, cfg: saml.SamlConfig | None = None):
    return saml.verify_response(response, cfg=cfg or _cfg(), acs_url=fx.ACS_URL)


def _reason(response: str, cfg: saml.SamlConfig | None = None) -> str:
    with pytest.raises(saml.SamlError) as caught:
        _verify(response, cfg)
    return caught.value.reason


# ── the positive case ─────────────────────────────────────────────────────

class TestAValidAssertion:
    def test_a_response_signed_assertion_verifies_and_maps(self):
        result = _verify(fx.signed_response())
        assert result.name_id == "persistent-user-1"
        assert result.email == "someone@acme.com"
        assert result.display_name == "Some One"
        assert result.groups == ["Claims-Team"]
        assert result.session_index == "session-1"

    def test_an_assertion_signed_assertion_verifies_too(self):
        """Signing the Assertion rather than the Response is equally standard —
        an SP that handles only one shape fails against half the market."""
        result = _verify(fx.signed_response(target="assertion"))
        assert result.email == "someone@acme.com"

    def test_a_bare_certificate_blob_is_accepted(self):
        """IdP admin consoles show the base64 without PEM armour, and that is
        what an administrator will paste."""
        armoured = _cfg(idp_x509_certs=[fx.IDP_CERT.decode()])
        assert _verify(fx.signed_response(), armoured).email == "someone@acme.com"

    def test_a_second_certificate_in_the_list_is_tried(self):
        """Certificate rollover must not be an outage: an IdP mid-rotation has
        two live signing certs and sends whichever it likes."""
        rolling = _cfg(idp_x509_certs=[fx.cert_body(fx.ROGUE_CERT), fx.cert_body()])
        assert _verify(fx.signed_response(), rolling).email == "someone@acme.com"

    def test_an_email_format_name_id_supplies_the_address(self):
        """Several IdPs send no separate email claim when the NameID is one."""
        xml = fx.response_xml(name_id="person@acme.com", email=None)
        result = _verify(fx.encode(fx.sign(xml)))
        assert result.email == "person@acme.com"


# ── signature ─────────────────────────────────────────────────────────────

class TestSignature:
    def test_an_unsigned_response_is_refused(self):
        assert _reason(fx.encode(fx.response_xml())) == "signature_invalid"

    def test_a_signature_by_the_wrong_key_is_refused(self):
        assert _reason(
            fx.signed_response(key=fx.ROGUE_KEY, cert=fx.ROGUE_CERT)
        ) == "signature_invalid"

    def test_a_certificate_we_do_not_hold_is_refused(self):
        """Presenting a valid signature with an attacker's own certificate is
        the whole attack if the SP trusts the embedded cert instead of its
        configured one."""
        assert _reason(
            fx.signed_response(key=fx.ROGUE_KEY, cert=fx.ROGUE_CERT),
            _cfg(idp_x509_certs=[fx.cert_body()]),
        ) == "signature_invalid"

    def test_content_edited_after_signing_is_refused(self):
        signed = fx.sign(fx.response_xml())
        root = etree.fromstring(signed)
        name_id = root.find(
            ".//{urn:oasis:names:tc:SAML:2.0:assertion}NameID")
        name_id.text = "somebody-else"
        assert _reason(fx.encode(etree.tostring(root))) == "signature_invalid"

    def test_a_provider_with_no_certificate_cannot_verify_anything(self):
        assert _reason(fx.signed_response(), _cfg(idp_x509_certs=[])) == "provider_incomplete"


# ── signature wrapping ────────────────────────────────────────────────────

class TestSignatureWrapping:
    """XSW is the defect class that has broken most SAML implementations.

    The attacker keeps a legitimately signed element and adds a forged one,
    betting that the verifier checks the first while the consumer reads the
    second. The defence here is structural — the consumer is only ever handed
    the subtree the signature covered — so these tests assert *which identity
    comes back*, not merely that something was rejected.
    """

    def test_a_forged_assertion_beside_a_signed_one_is_not_the_one_used(self):
        signed = fx.sign(fx.response_xml(name_id="real-user"), target="assertion")
        root = etree.fromstring(signed)
        forged = etree.fromstring(
            fx.response_xml(assertion_id="_forged", name_id="attacker",
                            email="attacker@evil.test").encode()
        ).find("./{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
        root.insert(0, forged)

        result = _verify(fx.encode(etree.tostring(root)))
        assert result.name_id == "real-user"
        assert result.email == "someone@acme.com"

    def test_a_signed_response_carrying_two_assertions_is_refused(self):
        """When the signature covers the Response, which assertion the IdP meant
        is genuinely ambiguous — and picking the first is how implementations get
        walked into accepting the attacker's."""
        xml = fx.response_xml()
        root = etree.fromstring(xml.encode())
        extra = etree.fromstring(
            fx.response_xml(assertion_id="_second", name_id="attacker").encode()
        ).find("./{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
        root.append(extra)
        signed = fx.sign(etree.tostring(root).decode())
        assert _reason(fx.encode(signed)) == "assertion_count"

    def test_a_signature_over_an_unrelated_element_is_not_a_login(self):
        """The signed subtree has to *be* the assertion or contain it."""
        from signxml import XMLSigner, methods

        wrapper = etree.fromstring(
            b'<Wrapper xmlns="urn:test"><Payload ID="_w1">x</Payload></Wrapper>')
        payload = wrapper.find("{urn:test}Payload")
        signer = XMLSigner(method=methods.enveloped, signature_algorithm="rsa-sha256",
                           digest_algorithm="sha256")
        signed_payload = signer.sign(payload, key=fx.IDP_KEY, cert=fx.IDP_CERT,
                                     reference_uri="_w1")
        root = etree.fromstring(fx.response_xml().encode())
        root.append(signed_payload)
        # Either refusal is correct and both are load-bearing: signxml declines
        # to resolve the reference in this document, and `_assertion_within`
        # would decline the element if it ever did.
        assert _reason(fx.encode(etree.tostring(root))) in {
            "unexpected_signed_element", "signature_invalid",
        }


# ── audience, destination, recipient ──────────────────────────────────────

class TestBinding:
    def test_an_assertion_for_another_service_provider_is_refused(self):
        assert _reason(
            fx.signed_response(audience="https://someone-else.test")
        ) == "audience_mismatch"

    def test_an_assertion_with_no_audience_is_refused(self):
        xml = fx.response_xml()
        root = etree.fromstring(xml.encode())
        restriction = root.find(
            ".//{urn:oasis:names:tc:SAML:2.0:assertion}AudienceRestriction")
        restriction.getparent().remove(restriction)
        assert _reason(fx.encode(fx.sign(etree.tostring(root).decode()))) == "audience_missing"

    def test_an_assertion_posted_to_the_wrong_endpoint_is_refused(self):
        assert _reason(
            fx.signed_response(destination="https://sp.test/somewhere-else")
        ) == "destination_mismatch"

    def test_a_recipient_naming_another_endpoint_is_refused(self):
        """Separate from Audience: one names who the assertion is for, the other
        where it was delivered. An assertion captured at one SP and replayed at
        another fails this even when the audience matches."""
        assert _reason(
            fx.signed_response(recipient="https://evil.test/acs")
        ) == "recipient_mismatch"

    def test_an_assertion_with_no_recipient_at_all_is_refused(self):
        """Required, not checked-when-present. The profile says a bearer
        confirmation MUST carry a Recipient and the SP MUST verify it; treating
        an absent one as 'nothing to check' left the only binding between an
        assertion and this endpoint optional at the sender's discretion.

        `Destination` is not a second line of defence: with an assertion-signed
        Response — the Entra and Okta default — it lives on the unsigned
        document, so a forger deletes it rather than matching it."""
        xml = fx.response_xml()
        root = etree.fromstring(xml.encode())
        data = root.find(
            ".//{urn:oasis:names:tc:SAML:2.0:assertion}SubjectConfirmationData")
        del data.attrib["Recipient"]
        assert _reason(
            fx.encode(fx.sign(etree.tostring(root).decode()))
        ) == "recipient_missing"

    def test_neither_destination_nor_recipient_is_no_longer_enough(self):
        xml = fx.response_xml()
        root = etree.fromstring(xml.encode())
        del root.attrib["Destination"]
        data = root.find(
            ".//{urn:oasis:names:tc:SAML:2.0:assertion}SubjectConfirmationData")
        del data.attrib["Recipient"]
        assert _reason(
            fx.encode(fx.sign(etree.tostring(root).decode(), target="assertion"))
        ) == "recipient_missing"

    def test_an_issuer_that_is_not_our_idp_is_refused(self):
        assert _reason(fx.signed_response(issuer="https://other-idp.test")) == "issuer_mismatch"


# ── timing ────────────────────────────────────────────────────────────────

class TestTiming:
    def test_an_expired_assertion_is_refused(self):
        assert _reason(
            fx.signed_response(not_on_or_after="2021-01-01T00:00:00Z")
        ) == "expired"

    def test_an_assertion_from_the_future_is_refused(self):
        assert _reason(fx.signed_response(not_before=fx.FAR_FUTURE)) == "not_yet_valid"

    def test_an_expired_subject_confirmation_is_refused(self):
        assert _reason(
            fx.signed_response(subject_expiry="2021-01-01T00:00:00Z")
        ) == "subject_expired"

    def test_clock_skew_is_honoured(self):
        """IdP and SP clocks differ; without skew a fresh assertion looks
        expired and SAML "randomly" fails for one deployment in ten."""
        just_expired = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        response = fx.signed_response(
            not_on_or_after=just_expired, subject_expiry=just_expired)
        assert _reason(response, _cfg(clock_skew_seconds=0)) == "expired"
        assert _verify(response, _cfg(clock_skew_seconds=120)).email == "someone@acme.com"


# ── parser hardening ──────────────────────────────────────────────────────

class TestHostileInput:
    def test_an_external_entity_is_not_resolved(self):
        """XXE on the login path would read files off the host, and the classic
        SAML variant smuggles the result into an attribute."""
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
            "<x>&xxe;</x></samlp:Response>"
        )
        assert _reason(base64.b64encode(payload.encode()).decode()) in {
            "malformed_xml", "not_a_response", "signature_invalid",
        }

    def test_an_entity_expansion_bomb_does_not_expand(self):
        entities = "".join(
            f'<!ENTITY e{i} "&e{i-1};&e{i-1};">' for i in range(1, 12))
        payload = (
            '<?xml version="1.0"?>'
            f'<!DOCTYPE r [<!ENTITY e0 "AAAAAAAAAA">{entities}]>'
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
            "<x>&e11;</x></samlp:Response>"
        )
        assert _reason(base64.b64encode(payload.encode()).decode()) in {
            "malformed_xml", "not_a_response", "signature_invalid",
        }

    def test_an_oversized_payload_is_dropped_before_parsing(self):
        assert _reason("A" * (saml.MAX_RESPONSE_BYTES + 4)) == "response_too_large"

    def test_a_non_base64_body_is_refused(self):
        assert _reason("this is not base64 !!!") == "malformed_base64"

    def test_an_empty_body_is_refused(self):
        assert _reason("") == "empty_response"

    def test_something_that_is_not_a_response_is_refused(self):
        assert _reason(fx.encode("<hello/>")) == "not_a_response"

    def test_an_idp_failure_status_is_reported_as_such(self):
        assert _reason(
            fx.signed_response(status="urn:oasis:names:tc:SAML:2.0:status:Requester")
        ) == "idp_refused"


# ── RelayState ────────────────────────────────────────────────────────────

class TestRelayState:
    """RelayState is echoed by the IdP and lands in a `Location` header, so an
    unchecked value is an open redirect on an endpoint that just issued a
    session. Signature and allowlist are both required: the signature stops a
    forged target, the allowlist bounds a legitimate one."""

    def test_a_minted_state_round_trips(self):
        assert saml.verify_relay_state(saml.mint_relay_state("/studio/cases"), _cfg()) \
            == "/studio/cases"

    def test_a_tampered_state_is_rejected(self):
        minted = saml.mint_relay_state("/studio/cases")
        version, encoded, expiry, sig = minted.split(".", 3)
        swapped = base64.urlsafe_b64encode(b"/studio/evil").decode().rstrip("=")
        assert saml.verify_relay_state(f"{version}.{swapped}.{expiry}.{sig}", _cfg()) is None

    def test_an_expired_state_is_rejected(self):
        assert saml.verify_relay_state(
            saml.mint_relay_state("/studio", ttl_seconds=-1), _cfg()) is None

    def test_a_target_outside_the_allowlist_is_rejected_even_though_we_signed_it(self):
        assert saml.verify_relay_state(saml.mint_relay_state("/admin"), _cfg()) is None

    @pytest.mark.parametrize("target", [
        "https://evil.test/steal",
        "//evil.test/steal",
        "/studio\\@evil.test",
        "/studio\nLocation: https://evil.test",
        "javascript:alert(1)",
        "studio/relative",
        # Every C0 control is stripped by a browser *before* the URL is parsed,
        # so each of these becomes `//evil.test`. Refusing `\n`, `\r` and `\`
        # by name missed the rest of the class — and `/login` signs whatever it
        # accepts, so this was a link an attacker could hand to a victim whose
        # completed sign-in then redirected, with the handoff code, to them.
        "/\t/evil.test",
        "/\x0b/evil.test",
        "/\x00/evil.test",
        "/ /evil.test",
    ])
    def test_targets_that_could_leave_the_site_are_refused(self, target):
        assert saml.relay_state_allowed(target, _cfg(allowed_relay_state_prefixes=["/"])) is False

    def test_garbage_never_raises(self):
        for value in ["", "...", "v1.a.b.c", "v9." + "x" * 50]:
            assert saml.verify_relay_state(value, _cfg()) is None


class TestTheUncheckedRedirect:
    """The prefix list is not only an allowlist — its **first entry is used as a
    redirect target**, by the ACS when the IdP sends no RelayState and by
    `/login` when no `target` is given. Nothing arrived from outside on that
    path, so `relay_state_allowed` never sees it, and the ACS redirect carries a
    live single-use sign-in code. A prefix a browser reads as another host would
    therefore hand that code to the host."""

    @pytest.mark.parametrize("prefix", [
        "//evil.test",           # scheme-relative
        "/\\evil.test",          # browsers normalise \ to / in http(s) URLs
        "/\tevil.test",          # tab is stripped before parsing
        "https://evil.test",
        "javascript:alert(1)",
        "studio",                # not absolute: resolves against the current path
    ])
    def test_a_prefix_that_leaves_the_origin_is_dropped_when_the_config_loads(self, prefix):
        cfg = saml.SamlConfig.from_row({"allowed_relay_state_prefixes": [prefix, "/studio"]})
        assert cfg.allowed_relay_state_prefixes == ["/studio"]
        assert saml.default_landing(cfg) == "/studio"

    def test_the_landing_page_falls_back_to_the_root_when_nothing_is_safe(self):
        cfg = saml.SamlConfig.from_row({"allowed_relay_state_prefixes": ["//evil.test"]})
        assert saml.default_landing(cfg) == "/"

    def test_the_landing_page_is_the_first_configured_prefix(self):
        cfg = saml.SamlConfig.from_row({"allowed_relay_state_prefixes": ["/", "/studio"]})
        assert saml.default_landing(cfg) == "/"

    @pytest.mark.parametrize("value,expected", [
        ("/studio", True),
        ("/", True),
        ("/idp/okta-lockup.svg", True),   # a hyphen is not a control character
        ("//evil.test", False),
        ("/\\evil.test", False),
        ("/ evil", False),
        ("/evil\x00", False),
        ("evil", False),
        ("", False),
    ])
    def test_is_safe_local_path(self, value, expected):
        assert saml.is_safe_local_path(value) is expected


class TestClockSkewIsBounded:
    """`clock_skew_seconds` widens the window in which an expired assertion is
    still accepted. Unbounded, it turns `NotOnOrAfter` off."""

    def test_a_huge_skew_is_clamped(self):
        cfg = saml.SamlConfig.from_row({"clock_skew_seconds": 10 ** 9})
        assert cfg.clock_skew_seconds == saml.MAX_CLOCK_SKEW_SECONDS

    def test_a_negative_skew_is_floored(self):
        assert saml.SamlConfig.from_row({"clock_skew_seconds": -5}).clock_skew_seconds == 0

    def test_junk_falls_back_to_the_default_rather_than_500ing_the_login(self):
        cfg = saml.SamlConfig.from_row({"clock_skew_seconds": "not a number"})
        assert cfg.clock_skew_seconds == saml.DEFAULT_CLOCK_SKEW_SECONDS


# ── metadata and AuthnRequest ─────────────────────────────────────────────

class TestOutbound:
    def test_metadata_names_our_entity_and_acs(self):
        xml = saml.build_metadata(sp_entity_id=fx.SP_ENTITY_ID, acs_url=fx.ACS_URL)
        root = etree.fromstring(xml)
        assert root.get("entityID") == fx.SP_ENTITY_ID
        acs = root.find(".//{urn:oasis:names:tc:SAML:2.0:metadata}AssertionConsumerService")
        assert acs.get("Location") == fx.ACS_URL
        assert acs.get("Binding").endswith("HTTP-POST")

    def test_metadata_does_not_advertise_a_key_we_do_not_use(self):
        """Publishing an encryption key invites the IdP to start encrypting
        assertions, which this SP cannot read."""
        xml = saml.build_metadata(sp_entity_id=fx.SP_ENTITY_ID, acs_url=fx.ACS_URL)
        assert b"KeyDescriptor" not in xml

    def test_an_authn_request_is_deflated_into_the_redirect(self):
        import zlib
        from urllib.parse import parse_qs, urlparse

        url, request_id = saml.build_authn_request(cfg=_cfg(), acs_url=fx.ACS_URL)
        query = parse_qs(urlparse(url).query)
        raw = base64.b64decode(query["SAMLRequest"][0])
        xml = zlib.decompress(raw, -15)
        assert request_id.startswith("_")
        assert b"AuthnRequest" in xml and fx.SP_ENTITY_ID.encode() in xml


# ── the endpoints ─────────────────────────────────────────────────────────

TENANT_SLUG = fx.TENANT_SLUG


async def _provider(session, **config_overrides) -> SsoProviderModel:
    tenant = TenantModel(slug=TENANT_SLUG, name="Acme")
    session.add(tenant)
    config = {
        "idp_entity_id": fx.IDP_ENTITY_ID,
        "idp_sso_url": "https://idp.test/sso",
        "idp_x509_certs": [fx.cert_body()],
        "sp_entity_id": fx.SP_ENTITY_ID,
        "acs_url": fx.ACS_URL,
        "email_domain_allowlist": ["acme.com"],
        "allowed_relay_state_prefixes": ["/studio"],
    }
    config.update(config_overrides)
    row = SsoProviderModel(
        tenant_id=TENANT_SLUG, provider="saml", client_id="unused",
        enabled=True, config=config,
    )
    session.add(row)
    await session.commit()
    tenant_identity.clear_cache()
    return row


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_metadata_is_served_to_an_anonymous_caller(self, session, anon_client):
        await _provider(session)
        resp = await anon_client.get(f"/api/v1/auth/saml/{TENANT_SLUG}/metadata")
        assert resp.status_code == 200
        assert b"EntityDescriptor" in resp.content

    @pytest.mark.asyncio
    async def test_an_unconfigured_tenant_answers_the_same_404(self, session, anon_client):
        """Uniform on every miss, so the endpoint is not a tenant-slug oracle."""
        await _provider(session)
        resp = await anon_client.get("/api/v1/auth/saml/no-such-tenant/metadata")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_login_redirects_to_the_idp(self, session, anon_client):
        await _provider(session)
        resp = await anon_client.get(
            f"/api/v1/auth/saml/{TENANT_SLUG}/login", params={"target": "/studio/cases"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("https://idp.test/sso?")

    @pytest.mark.asyncio
    async def test_login_refuses_a_destination_off_the_allowlist(self, session, anon_client):
        """Checked before minting, so a target that would not be honoured on the
        way back is never signed in the first place."""
        await _provider(session)
        resp = await anon_client.get(
            f"/api/v1/auth/saml/{TENANT_SLUG}/login",
            params={"target": "https://evil.test"}, follow_redirects=False,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_the_acs_provisions_and_hands_back_a_code_not_a_token(
        self, session, anon_client,
    ):
        await _provider(session)
        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(),
                  "RelayState": saml.mint_relay_state("/studio")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/studio?saml_code=")
        # A refresh token in a URL ends up in history, in `Referer`, and in every
        # proxy log on the way.
        assert "access_token" not in location and "refresh_token" not in location

        user = (await session.execute(
            select(HelixUserModel).where(
                HelixUserModel.external_id == "persistent-user-1")
        )).scalars().first()
        assert user is not None and user.email == "someone@acme.com"

    @pytest.mark.asyncio
    async def test_an_assertion_cannot_be_posted_into_another_tenant(
        self, session, anon_client,
    ):
        """`Destination` and `Recipient` are compared against the provider's
        configured ACS URL — a fixed string — so they pass whichever `{tenant}`
        segment the request actually arrived on. The IdP posts to one URL, but a
        person can lift their own valid assertion out of the browser and post it
        anywhere. Without this check a federated user picks their own tenant.
        """
        await _provider(session)
        session.add(TenantModel(slug="other-tenant", name="Other"))
        await session.commit()
        tenant_identity.clear_cache()

        resp = await anon_client.post(
            "/api/v1/auth/saml/other-tenant/acs",
            data={"SAMLResponse": fx.signed_response()}, follow_redirects=False,
        )
        assert resp.status_code == 404
        assert (await session.execute(select(HelixUserModel).where(
            HelixUserModel.external_id == "persistent-user-1"))).scalars().first() is None

    @pytest.mark.asyncio
    async def test_the_handoff_code_works_exactly_once(self, session, anon_client):
        await _provider(session)
        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response()}, follow_redirects=False,
        )
        code = resp.headers["location"].split("saml_code=")[1]

        first = await anon_client.post("/api/v1/auth/saml/exchange", json={"code": code})
        assert first.status_code == 200
        assert first.json()["access_token"]

        second = await anon_client.post("/api/v1/auth/saml/exchange", json={"code": code})
        assert second.status_code == 400

    @pytest.mark.asyncio
    async def test_an_unknown_handoff_code_is_refused(self, session, anon_client):
        await _provider(session)
        resp = await anon_client.post(
            "/api/v1/auth/saml/exchange", json={"code": "not-a-real-code"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_a_replayed_assertion_is_refused(self, session, anon_client):
        """Every other control on an assertion is a five-minute window, inside
        which the same signed bytes log in as often as they are posted."""
        await _provider(session)
        payload = {"SAMLResponse": fx.signed_response()}

        first = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs", data=payload, follow_redirects=False)
        assert first.status_code == 303

        second = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs", data=payload, follow_redirects=False)
        assert second.status_code == 400

        rows = (await session.execute(select(SamlAssertionReplayModel))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_an_unsigned_assertion_does_not_log_anyone_in(self, session, anon_client):
        await _provider(session)
        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.encode(fx.response_xml())}, follow_redirects=False,
        )
        assert resp.status_code == 400
        assert (await session.execute(select(HelixUserModel).where(
            HelixUserModel.external_id == "persistent-user-1"))).scalars().first() is None

    @pytest.mark.asyncio
    async def test_a_domain_off_the_allowlist_cannot_provision(self, session, anon_client):
        await _provider(session, email_domain_allowlist=["only-this.test"])
        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response()}, follow_redirects=False,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_auto_provision_off_refuses_an_unknown_person(self, session, anon_client):
        await _provider(session, auto_provision=False)
        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response()}, follow_redirects=False,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a_known_account_logs_in_even_when_the_allowlist_tightens(
        self, session, anon_client,
    ):
        """The allowlist gates provisioning, not login — otherwise narrowing it
        locks out the accounts an administrator created deliberately."""
        await _provider(session, email_domain_allowlist=[])
        session.add(HelixUserModel(
            username="known", email="someone@acme.com", roles=["viewer"],
            is_active=True, external_id="persistent-user-1",
        ))
        await session.commit()

        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response()}, follow_redirects=False,
        )
        assert resp.status_code == 303

    @pytest.mark.asyncio
    async def test_a_disabled_account_cannot_sign_in_with_saml(self, session, anon_client):
        await _provider(session)
        session.add(HelixUserModel(
            username="disabled", email="someone@acme.com", roles=["viewer"],
            is_active=False, external_id="persistent-user-1",
        ))
        await session.commit()

        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response()}, follow_redirects=False,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_the_refusal_never_says_which_check_failed(self, session, anon_client):
        """Anyone can POST here, so a reason per failure lets a forger fix one
        thing at a time."""
        await _provider(session)
        bodies = [
            fx.encode(fx.response_xml()),                      # unsigned
            fx.signed_response(audience="https://elsewhere"),  # wrong audience
            fx.signed_response(destination="https://elsewhere"),
        ]
        details = set()
        for body in bodies:
            resp = await anon_client.post(
                f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
                data={"SAMLResponse": body}, follow_redirects=False,
            )
            assert resp.status_code == 400
            details.add(resp.json().get("detail", {}).get("message")
                        if isinstance(resp.json().get("detail"), dict)
                        else str(resp.json().get("detail")))
        assert len(details) == 1, details

    @pytest.mark.asyncio
    async def test_the_test_endpoint_needs_an_admin_and_issues_nothing(
        self, session, client, anon_client,
    ):
        await _provider(session)
        assert (await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/test",
            json={"saml_response": fx.signed_response()},
        )).status_code in (401, 403)

        resp = await client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/test",
            json={"saml_response": fx.signed_response()},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True and body["email"] == "someone@acme.com"
        assert "access_token" not in body
        # The dry run must not burn the assertion for the real path.
        assert (await session.execute(
            select(SamlAssertionReplayModel))).scalars().first() is None


class TestAccountAdoption:
    """An assertion may not become an account it only matches by email.

    `saml_acs` looks for an existing user by `external_id`; when that misses,
    provisioning's lookup used to fall through to a case-insensitive email
    match and return whatever local account held the address. **Proven live: an
    assertion with a brand-new NameID and the email of a local `admin` logged in
    as that admin, no password involved.** The domain allowlist is no defence —
    the allowlisted domain is the corporate one the addresses are in.
    """

    @staticmethod
    async def _local_admin(session, email: str = "localadmin@acme.com"):
        user = HelixUserModel(
            username="localadmin", email=email, display_name="Local Admin",
            roles=["admin"], is_active=True, password_hash="$2b$12$local",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    @pytest.mark.asyncio
    async def test_an_assertion_cannot_sign_in_as_a_local_account(
        self, session, anon_client,
    ):
        await _provider(session)
        admin = await self._local_admin(session)

        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_adopt", name_id="attacker-99",
                email="localadmin@acme.com")},
            follow_redirects=False,
        )
        assert resp.status_code == 403

        await session.refresh(admin)
        # Untouched: not linked to the IdP, and no session was minted for it.
        assert admin.external_id is None
        assert (await session.execute(select(AuthOtpModel).where(
            AuthOtpModel.user_id == admin.id))).scalars().first() is None

    @pytest.mark.asyncio
    async def test_the_refusal_is_recorded_as_a_security_event(
        self, session, anon_client,
    ):
        await _provider(session)
        await self._local_admin(session)
        await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_adopt2", name_id="attacker-98",
                email="localadmin@acme.com")},
            follow_redirects=False,
        )
        rows = (await session.execute(select(IdentityProvisionLogModel))).scalars().all()
        assert any(r.result == "error" and (r.detail or {}).get("reason") == "email_taken"
                   for r in rows)

    @pytest.mark.asyncio
    async def test_a_known_external_id_still_signs_in(self, session, anon_client):
        """The fix must not break the case it exists to protect: an account that
        IS this federated identity is matched by `external_id`, email or not."""
        await _provider(session)
        user = HelixUserModel(
            username="fed", email="fed@acme.com", display_name="Fed",
            external_id="persistent-user-1", roles=["viewer"], is_active=True,
        )
        session.add(user)
        await session.commit()

        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_known", email="fed@acme.com")},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    @pytest.mark.asyncio
    async def test_the_opt_in_knob_still_refuses_a_password_account(
        self, session, anon_client,
    ):
        """`link_existing_by_email` restores the old convenience — but never for
        an account with a local password, which is somebody who signs in here."""
        await _provider(session, link_existing_by_email=True)
        admin = await self._local_admin(session)

        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_optin", name_id="attacker-97",
                email="localadmin@acme.com")},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        await session.refresh(admin)
        assert admin.external_id is None

    @pytest.mark.asyncio
    async def test_the_opt_in_knob_links_a_passwordless_account(
        self, session, anon_client,
    ):
        await _provider(session, link_existing_by_email=True)
        user = HelixUserModel(
            username="invited", email="invited@acme.com", display_name="Invited",
            roles=["viewer"], is_active=True, password_hash=None,
        )
        session.add(user)
        await session.commit()

        resp = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_optin2", name_id="new-nameid-1",
                email="invited@acme.com")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        await session.refresh(user)
        assert user.external_id == "new-nameid-1"

    @pytest.mark.asyncio
    async def test_the_tester_predicts_the_acs_decision(
        self, session, client, anon_client,
    ):
        """The dry run has to agree with the live path.

        A tester answering "would provision" where the ACS answers 403 sends the
        administrator hunting through an attribute map for a fault that is not
        there — so every refusal the ACS can reach has to be named here.
        """
        await _provider(session)
        await self._local_admin(session)

        held = (await client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/test",
            json={"saml_response": fx.signed_response(
                assertion_id="_t1", name_id="attacker-1",
                email="localadmin@acme.com")},
        )).json()
        assert held["outcome"] == "email_taken"
        assert held["would_provision"] is False
        assert held["email_held_by"] == "localadmin"

        fresh = (await client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/test",
            json={"saml_response": fx.signed_response(
                assertion_id="_t2", name_id="new-1", email="new@acme.com")},
        )).json()
        assert fresh["outcome"] == "would_create" and fresh["would_provision"] is True

        outside = (await client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/test",
            json={"saml_response": fx.signed_response(
                assertion_id="_t3", name_id="new-2", email="new@elsewhere.com")},
        )).json()
        assert outside["outcome"] == "domain_not_allowed"

        # And the tester still issues nothing on any of those paths.
        assert (await session.execute(
            select(SamlAssertionReplayModel))).scalars().first() is None

    @pytest.mark.asyncio
    async def test_an_administrator_can_link_the_account_and_then_it_signs_in(
        self, session, client, anon_client,
    ):
        """The refusal has to have a way out, or SSO is unusable for every
        customer whose staff already had accounts. Link, then the very assertion
        that was refused signs in as that account — and keeps its roles."""
        await _provider(session)
        admin = await self._local_admin(session)

        refused = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_pre-link", name_id="idp-nameid-77",
                email="localadmin@acme.com")},
            follow_redirects=False,
        )
        assert refused.status_code == 403

        linked = await client.post("/api/v1/identity/provisioning/links", json={
            "identifier": "localadmin@acme.com", "external_id": "idp-nameid-77"})
        assert linked.status_code == 200

        accepted = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_post-link", name_id="idp-nameid-77",
                email="localadmin@acme.com")},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        code = accepted.headers["location"].split("saml_code=")[-1]
        tokens = await anon_client.post("/api/v1/auth/saml/exchange", json={"code": code})
        assert tokens.status_code == 200
        assert tokens.json()["user"]["username"] == admin.username

    @pytest.mark.asyncio
    async def test_sp_initiated_login_lands_where_the_provider_says(
        self, session, anon_client,
    ):
        """The landing path comes from the provider, not from a constant.

        It was hardcoded `/studio`, which is not a route in the Studio SPA: the
        sign-in completed, the ACS redirected to `/studio?saml_code=…`, and the
        browser landed on a page the app does not render — so nothing redeemed
        the code and a working login looked broken. Seen live against Okta.
        """
        await _provider(session, allowed_relay_state_prefixes=["/"])

        resp = await anon_client.get(
            f"/api/v1/auth/saml/{TENANT_SLUG}/login", follow_redirects=False)
        assert resp.status_code == 302
        relay = resp.headers["location"].split("RelayState=")[-1]
        assert saml.verify_relay_state(
            unquote(relay), saml.SamlConfig(allowed_relay_state_prefixes=["/"]),
        ) == "/"

        # And the ACS agrees: no RelayState at all lands on the same place.
        landed = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_landing", name_id="lander-1",
                email="lander@acme.com")},
            follow_redirects=False)
        assert landed.status_code == 303
        assert landed.headers["location"].startswith("/?saml_code=")

    @pytest.mark.asyncio
    async def test_login_will_not_sign_a_target_a_browser_reads_as_another_host(
        self, session, anon_client,
    ):
        """`/login` is anonymous and mints a **signed** RelayState for whatever
        it accepts, so an accepted target is a link an attacker can hand to a
        victim: their real sign-in completes and the ACS redirects to the
        attacker's host with the handoff code. `/\\t/evil.test` was accepted —
        no backslash, no leading `//`, matches a `/` prefix — and a browser
        strips the tab before parsing, making it `//evil.test`."""
        await _provider(session, allowed_relay_state_prefixes=["/"])
        for target in ["/\t/evil.test", "/\x0b/evil.test", "//evil.test"]:
            resp = await anon_client.get(
                f"/api/v1/auth/saml/{TENANT_SLUG}/login",
                params={"target": target}, follow_redirects=False,
            )
            assert resp.status_code == 400, target

    @pytest.mark.asyncio
    async def test_an_idp_initiated_login_cannot_be_redirected_off_the_origin(
        self, session, anon_client,
    ):
        """The one redirect nothing validates, because nothing arrived.

        A `//evil.test` landing prefix would have put an attacker-chosen host in
        the `Location`, and the query string it carries is a live single-use
        sign-in code — the whole account, handed over by a valid login.
        `relay_state_allowed` never sees this value: there is no RelayState.
        """
        await _provider(session, allowed_relay_state_prefixes=["//evil.test", "/studio"])

        landed = await anon_client.post(
            f"/api/v1/auth/saml/{TENANT_SLUG}/acs",
            data={"SAMLResponse": fx.signed_response(
                assertion_id="_offsite", name_id="offsite-1",
                email="offsite@acme.com")},
            follow_redirects=False)
        assert landed.status_code == 303
        location = landed.headers["location"]
        assert "evil.test" not in location
        assert location.startswith("/studio?saml_code=")
