"""SAML IdP metadata import and the scheduled certificate refresh.

Two properties carry the whole design, and both are about what the refresh
*will not* do:

* it only ever **adds** certificates, so a truncated or wrong response cannot
  strip the one that currently verifies;
* it **never** applies a changed issuer or sign-in URL, because a single fetch
  that could repoint where users authenticate is a takeover primitive, not a
  convenience.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from user_service.identity import saml_metadata as md
from tests.phase_identity import _saml_fixtures as fx

ENTITY = "https://sts.windows.net/f905f370-11cf-47f7-8764-9faa2de3b370/"
SSO = "https://login.microsoftonline.com/f905f370/saml2"


def _doc(*, certs: list[str] | None = None, entity: str = ENTITY, sso: str = SSO,
         slo: bool = True, idp: bool = True, use: str = "signing") -> bytes:
    """An Entra-shaped metadata document, built to order."""
    certs = certs if certs is not None else [fx.cert_body()]
    keys = "".join(
        f'<KeyDescriptor use="{use}"><KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#">'
        f"<X509Data><X509Certificate>{c}</X509Certificate></X509Data></KeyInfo>"
        f"</KeyDescriptor>"
        for c in certs
    )
    slo_xml = (
        '<SingleLogoutService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="{sso}"/>' if slo else "")
    descriptor = (
        f'<IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        f"{keys}{slo_xml}"
        f'<SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{sso}-post"/>'
        f'<SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="{sso}"/>'
        f"</IDPSSODescriptor>"
    ) if idp else (
        '<SPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol"/>')
    return (
        '<?xml version="1.0"?>'
        f'<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{entity}">'
        f"{descriptor}</EntityDescriptor>"
    ).encode()


class TestParsing:
    def test_reads_the_three_fields_setup_actually_needs(self):
        out = md.parse_metadata(_doc())
        assert out["idp_entity_id"] == ENTITY
        assert out["idp_x509_certs"] == [fx.cert_body()]

    def test_prefers_the_redirect_binding_we_actually_emit(self):
        """`build_authn_request` uses HTTP-Redirect; taking the POST location
        would produce a provider that 400s at the IdP."""
        assert md.parse_metadata(_doc())["idp_sso_url"] == SSO

    def test_an_encryption_key_is_not_offered_as_a_signing_certificate(self):
        """Publishing an encryption key does not mean the IdP signs with it —
        feeding it to the verifier only adds a failing attempt per login."""
        with pytest.raises(md.MetadataError):
            md.parse_metadata(_doc(use="encryption"))

    def test_an_sp_document_is_refused_with_a_usable_reason(self):
        """The commonest paste error is one's own SP metadata."""
        with pytest.raises(md.MetadataError, match="service provider"):
            md.parse_metadata(_doc(idp=False))

    def test_a_federation_document_yields_its_idp(self):
        one = _doc().decode().replace('<?xml version="1.0"?>', "")
        wrapped = (
            '<?xml version="1.0"?>'
            '<EntitiesDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata">'
            f"{one}</EntitiesDescriptor>"
        ).encode()
        assert md.parse_metadata(wrapped)["idp_entity_id"] == ENTITY

    @pytest.mark.parametrize("payload", [b"", b"not xml", b"<a/>", b"<EntityDescriptor/>"])
    def test_junk_is_a_refusal_not_a_crash(self, payload):
        with pytest.raises(md.MetadataError):
            md.parse_metadata(payload)

    def test_the_parser_cannot_be_talked_into_reading_a_local_file(self):
        """Metadata is attacker-influenced input reaching an XML parser — the
        same XXE exposure the assertion path closes, on a path that also makes
        outbound requests."""
        evil = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" '
            'entityID="&xxe;"><IDPSSODescriptor/></EntityDescriptor>'
        ).encode()
        with pytest.raises(md.MetadataError):
            md.parse_metadata(evil)

    def test_oversize_is_refused_before_parsing(self):
        with pytest.raises(md.MetadataError, match="larger than"):
            md.parse_metadata(b"<x/>" + b" " * (md.MAX_METADATA_BYTES + 1))


class TestCertificates:
    def test_expiry_is_read_from_the_certificate(self):
        health = md.cert_health([fx.cert_body()])
        assert health[0]["not_after"] and health[0]["expired"] is False

    def test_a_pem_armoured_certificate_is_read_too(self):
        """Found live, not by a test: the hand-configured Okta provider stored a
        full `-----BEGIN CERTIFICATE-----` block — which `_pem()` accepts and the
        verifier is happy with — so expiry read as unknown and that provider
        would never have warned before it lapsed."""
        pem = ("-----BEGIN CERTIFICATE-----\n"
               + "\n".join(fx.cert_body()[i:i + 64] for i in range(0, len(fx.cert_body()), 64))
               + "\n-----END CERTIFICATE-----\n")
        armoured, bare = md.cert_health([pem])[0], md.cert_health([fx.cert_body()])[0]
        assert armoured["not_after"] == bare["not_after"] is not None
        assert armoured["fingerprint"] == bare["fingerprint"], \
            "the label must come from the payload, not the armour"

    def test_an_unreadable_certificate_does_not_break_the_warning_screen(self):
        health = md.cert_health(["not-a-certificate"])
        assert health[0]["not_after"] is None and health[0]["days_remaining"] is None

    def test_a_refresh_only_ever_adds(self):
        """The dangerous direction. A truncated response, a half-published
        rollover or the wrong URL must not remove the cert that works."""
        assert md.merge_certs(["A", "B"], ["C"]) == ["A", "B", "C"]
        assert md.merge_certs(["A", "B"], []) == ["A", "B"]
        assert md.merge_certs(["A"], ["A"]) == ["A"]


class TestRefreshPlan:
    BASE = {"idp_entity_id": ENTITY, "idp_sso_url": SSO, "idp_x509_certs": ["OLD"]}

    def test_a_rollover_certificate_is_added_silently(self):
        new_cfg, report = md.plan_refresh(self.BASE, {
            "idp_entity_id": ENTITY, "idp_sso_url": SSO, "idp_x509_certs": ["OLD", "NEW"]})
        assert new_cfg["idp_x509_certs"] == ["OLD", "NEW"]
        assert report["certs_added"] == 1 and not report["drift"]

    def test_a_changed_issuer_is_reported_and_not_applied(self):
        new_cfg, report = md.plan_refresh(self.BASE, {
            "idp_entity_id": "https://evil.test/", "idp_sso_url": SSO,
            "idp_x509_certs": ["OLD"]})
        assert new_cfg["idp_entity_id"] == ENTITY, "the trusted issuer must not move"
        assert report["drift"]["idp_entity_id"]["published"] == "https://evil.test/"

    def test_a_changed_sign_in_url_is_reported_and_not_applied(self):
        new_cfg, report = md.plan_refresh(self.BASE, {
            "idp_entity_id": ENTITY, "idp_sso_url": "https://evil.test/saml2",
            "idp_x509_certs": ["OLD"]})
        assert new_cfg["idp_sso_url"] == SSO, "where users authenticate must not move"
        assert "idp_sso_url" in report["drift"]

    def test_a_failure_changes_nothing_but_the_note(self):
        after = md.record_failure(self.BASE, "Could not reach that URL")
        assert after["idp_x509_certs"] == ["OLD"]
        assert after["idp_entity_id"] == ENTITY
        assert "Could not reach" in after["metadata_sync"]["last_error"]


class TestFetchGuard:
    @pytest.mark.asyncio
    async def test_plain_http_is_refused(self):
        with pytest.raises(md.MetadataError, match="https"):
            await md.fetch_metadata("http://idp.test/metadata")

    @pytest.mark.asyncio
    async def test_an_internal_address_is_refused_by_the_ssrf_guard(self):
        """The endpoint makes this server fetch a URL somebody typed."""
        with pytest.raises(md.MetadataError):
            await md.fetch_metadata("https://127.0.0.1/metadata")

    @pytest.mark.asyncio
    async def test_neither_half_supplied_is_a_refusal(self):
        with pytest.raises(md.MetadataError):
            await md.load_metadata()


class TestTheSweep:
    """`refresh_all` against a real row — the part the pure planner cannot show."""

    async def _row(self, session, **cfg):
        from velaris_shared.db.models import SsoProviderModel

        base = {"idp_entity_id": ENTITY, "idp_sso_url": SSO, "idp_x509_certs": ["OLD"],
                "sp_entity_id": "velaris-studio",
                "acs_url": "https://velaris.test/api/v1/auth/saml/acme/acs",
                "idp_metadata_url": "https://idp.test/metadata"}
        base.update(cfg)
        row = SsoProviderModel(tenant_id="acme", provider="saml", client_id="x",
                               enabled=True, config=base)
        session.add(row)
        await session.commit()
        return row

    @pytest.mark.asyncio
    async def test_a_new_certificate_lands_on_the_row(self, session, monkeypatch):
        row = await self._row(session)

        async def _fake(url=None, xml=None):
            return {"idp_entity_id": ENTITY, "idp_sso_url": SSO,
                    "idp_x509_certs": ["OLD", "NEW"], "idp_slo_url": None,
                    "name_id_format": None}
        monkeypatch.setattr(md, "load_metadata", _fake)

        await md.refresh_all(session)
        await session.refresh(row)
        assert row.config["idp_x509_certs"] == ["OLD", "NEW"]
        assert row.config["metadata_sync"]["last_error"] is None

    @pytest.mark.asyncio
    async def test_an_unreachable_idp_leaves_a_working_provider_alone(
        self, session, monkeypatch,
    ):
        """The whole point of the fallback: a blip must not break sign-in."""
        row = await self._row(session)

        async def _boom(url=None, xml=None):
            raise md.MetadataError("Could not reach that URL: timeout")
        monkeypatch.setattr(md, "load_metadata", _boom)

        await md.refresh_all(session)
        await session.refresh(row)
        assert row.config["idp_x509_certs"] == ["OLD"]
        assert row.config["idp_sso_url"] == SSO
        assert row.enabled is True
        assert "timeout" in row.config["metadata_sync"]["last_error"]

    @pytest.mark.asyncio
    async def test_a_provider_with_no_metadata_url_is_left_untouched(
        self, session, monkeypatch,
    ):
        row = await self._row(session, idp_metadata_url="")

        async def _fake(url=None, xml=None):
            raise AssertionError("must not fetch for a row with no metadata URL")
        monkeypatch.setattr(md, "load_metadata", _fake)

        await md.refresh_all(session)
        await session.refresh(row)
        assert "metadata_sync" not in row.config


class TestWriteTimeValidation:
    @pytest.mark.asyncio
    async def test_an_internal_metadata_url_cannot_be_saved(self, client):
        """Validated on write as well as before each fetch: a URL this server
        GETs on a timer must not be checkable in only one of those places."""
        r = await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "saml", "client_id": "velaris-studio", "client_secret": "",
            "config": {
                "idp_entity_id": ENTITY, "idp_sso_url": SSO,
                "idp_x509_certs": [fx.cert_body()], "sp_entity_id": "velaris-studio",
                "acs_url": "https://velaris.test/api/v1/auth/saml/acme/acs",
                "idp_metadata_url": "http://169.254.169.254/latest/meta-data/",
            },
        })
        assert r.status_code == 400
        assert "metadata URL" in r.text


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_pasted_xml_is_parsed_and_nothing_is_saved(self, client, session):
        from sqlalchemy import select

        from velaris_shared.db.models import SsoProviderModel

        r = await client.post("/api/v1/auth/saml/metadata/parse",
                              json={"xml": _doc().decode()})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["idp_entity_id"] == ENTITY
        assert body["source"] == "xml"
        assert body["certs"][0]["expired"] is False

        rows = (await session.execute(select(SsoProviderModel))).scalars().all()
        assert rows == [], "importing metadata must not create or edit a provider"

    @pytest.mark.asyncio
    async def test_a_bad_document_answers_a_reason_the_admin_can_act_on(self, client):
        r = await client.post("/api/v1/auth/saml/metadata/parse",
                              json={"xml": _doc(idp=False).decode()})
        assert r.status_code == 400
        assert "service provider" in r.text

    @pytest.mark.asyncio
    async def test_it_is_not_open_to_anonymous_callers(self, anon_client):
        r = await anon_client.post("/api/v1/auth/saml/metadata/parse",
                                   json={"xml": _doc().decode()})
        assert r.status_code in (401, 403)
