"""Assertions shaped the way Entra ID and Okta actually emit them.

**What this is, and what it is not.** A real tenant's signed capture cannot live
in a test: it expires, it carries somebody's real identifiers, and any negative
case has to re-sign it anyway. So these are the *documents* — namespace prefixes,
claim URIs, attribute value typing, which element carries the signature, what
goes in a group value — reproduced from what the two products emit, and signed
with the throwaway key in :mod:`_saml_fixtures`.

That covers every part of the flow that is ours: parsing, namespace handling,
attribute mapping, group mapping, provisioning. It does **not** cover their
signing keys or their exact canonicalisation, which only a live tenant proves.

The differences that matter, and why each is worth a test:

* **Okta uses `saml2:` / `saml2p:` prefixes; Entra uses a default namespace with
  no prefix at all.** A parser that pattern-matches prefixes rather than
  resolving namespaces handles one and silently fails the other.
* **Okta signs the Response, Entra signs the Assertion** — both shapes have to
  verify, and the consumer must read from whichever subtree was signed.
* **Okta sends `firstName`/`lastName` and no display name**; Entra sends
  `http://schemas.microsoft.com/identity/claims/displayname`.
* **Entra's group values are directory object GUIDs, not names.** An
  administrator therefore maps GUIDs, and a deployment expecting names maps
  nothing at all.
* **Okta types its attribute values** (`xsi:type="xs:string"`), which puts an
  extra namespace declaration inside every value element.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from tests.phase_identity import _saml_fixtures as fx

#: Entra's issuer is its tenant STS, and the trailing slash is part of it.
ENTRA_TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
ENTRA_ISSUER = f"https://sts.windows.net/{ENTRA_TENANT_ID}/"
#: Entra names the audience with the application ID URI the admin configured.
ENTRA_AUDIENCE = "api://velaris-sp"
#: A directory group is asserted by object id. This is the whole reason the
#: mapping table exists rather than a name match.
ENTRA_GROUP_IDS = (
    "0a1b2c3d-4e5f-6789-abcd-ef0123456789",
    "9f8e7d6c-5b4a-3928-1716-fedcba987654",
)

OKTA_ISSUER = "http://www.okta.com/exk1fq9wpsxRNQvGT697"
OKTA_AUDIENCE = fx.SP_ENTITY_ID

FAR_FUTURE = fx.FAR_FUTURE
LONG_PAST = fx.LONG_PAST


def entra_response_xml(
    *,
    assertion_id: str = "_entra-assertion-1",
    name_id: str = "IPTOxLNTz9k2mS7cVsQ9KJ0mI0Bz3nQ4bXwqfKQZlpM",
    email: str = "ada@acme.com",
    display_name: str = "Ada Lovelace",
    groups: tuple[str, ...] = ENTRA_GROUP_IDS,
    audience: str = ENTRA_AUDIENCE,
    destination: str = fx.ACS_URL,
    recipient: str = fx.ACS_URL,
) -> str:
    """An Entra ID SAML 2.0 Response, with Entra's default-namespace shape.

    Note there is no prefix on the assertion elements at all — they inherit the
    default `xmlns`. `InResponseTo` and `SessionNotOnOrAfter` are included
    because Entra sends them and a parser that trips over unexpected attributes
    should do so here rather than in production.
    """
    group_values = "".join(
        f"<AttributeValue>{g}</AttributeValue>" for g in groups
    )
    return f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" ID="_response-1" Version="2.0" IssueInstant="2026-08-16T09:00:00.000Z" Destination="{destination}" InResponseTo="_authnrequest-1">
<Issuer xmlns="urn:oasis:names:tc:SAML:2.0:assertion">{ENTRA_ISSUER}</Issuer>
<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
<Assertion xmlns="urn:oasis:names:tc:SAML:2.0:assertion" ID="{assertion_id}" Version="2.0" IssueInstant="2026-08-16T09:00:00.000Z">
<Issuer>{ENTRA_ISSUER}</Issuer>
<Subject>
<NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent">{name_id}</NameID>
<SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
<SubjectConfirmationData InResponseTo="_authnrequest-1" NotOnOrAfter="{FAR_FUTURE}" Recipient="{recipient}"/>
</SubjectConfirmation>
</Subject>
<Conditions NotBefore="{LONG_PAST}" NotOnOrAfter="{FAR_FUTURE}">
<AudienceRestriction><Audience>{audience}</Audience></AudienceRestriction>
</Conditions>
<AttributeStatement>
<Attribute Name="http://schemas.microsoft.com/identity/claims/tenantid"><AttributeValue>{ENTRA_TENANT_ID}</AttributeValue></Attribute>
<Attribute Name="http://schemas.microsoft.com/identity/claims/objectidentifier"><AttributeValue>ee2c5a3f-1b7d-4c2a-9f31-2b6e0d4a7c11</AttributeValue></Attribute>
<Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"><AttributeValue>{email}</AttributeValue></Attribute>
<Attribute Name="http://schemas.microsoft.com/identity/claims/displayname"><AttributeValue>{display_name}</AttributeValue></Attribute>
<Attribute Name="http://schemas.microsoft.com/ws/2008/06/identity/claims/groups">{group_values}</Attribute>
</AttributeStatement>
<AuthnStatement AuthnInstant="2026-08-16T08:59:00.000Z" SessionIndex="{assertion_id}">
<AuthnContext><AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</AuthnContextClassRef></AuthnContext>
</AuthnStatement>
</Assertion>
</samlp:Response>"""


def okta_response_xml(
    *,
    assertion_id: str = "id-okta-assertion-1",
    name_id: str = "ada@acme.com",
    email: str = "ada@acme.com",
    first_name: str = "Ada",
    last_name: str = "Lovelace",
    groups: tuple[str, ...] = ("Claims-Team", "Everyone"),
    audience: str = OKTA_AUDIENCE,
    destination: str = fx.ACS_URL,
    recipient: str = fx.ACS_URL,
) -> str:
    """An Okta SAML 2.0 Response: `saml2:` prefixes and typed attribute values.

    Okta's default app sends `firstName` and `lastName` and **no** display-name
    claim, which is why the SP composes one; and its NameID is the email address
    in `emailAddress` format, which is why an assertion with no email claim can
    still yield an address.
    """
    def attribute(name: str, *values: str) -> str:
        body = "".join(
            '<saml2:AttributeValue xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'xsi:type="xs:string">{v}</saml2:AttributeValue>'
            for v in values
        )
        return (
            f'<saml2:Attribute Name="{name}" '
            'NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:unspecified">'
            f"{body}</saml2:Attribute>"
        )

    attributes = "".join([
        attribute("email", email),
        attribute("firstName", first_name),
        attribute("lastName", last_name),
        attribute("groups", *groups),
    ])
    return f"""<saml2p:Response xmlns:saml2p="urn:oasis:names:tc:SAML:2.0:protocol" Destination="{destination}" ID="id-okta-response-1" IssueInstant="2026-08-16T09:00:00.000Z" Version="2.0">
<saml2:Issuer xmlns:saml2="urn:oasis:names:tc:SAML:2.0:assertion" Format="urn:oasis:names:tc:SAML:2.0:nameid-format:entity">{OKTA_ISSUER}</saml2:Issuer>
<saml2p:Status><saml2p:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></saml2p:Status>
<saml2:Assertion xmlns:saml2="urn:oasis:names:tc:SAML:2.0:assertion" ID="{assertion_id}" IssueInstant="2026-08-16T09:00:00.000Z" Version="2.0">
<saml2:Issuer Format="urn:oasis:names:tc:SAML:2.0:nameid-format:entity">{OKTA_ISSUER}</saml2:Issuer>
<saml2:Subject>
<saml2:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{name_id}</saml2:NameID>
<saml2:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
<saml2:SubjectConfirmationData InResponseTo="id-authnrequest-1" NotOnOrAfter="{FAR_FUTURE}" Recipient="{recipient}"/>
</saml2:SubjectConfirmation>
</saml2:Subject>
<saml2:Conditions NotBefore="{LONG_PAST}" NotOnOrAfter="{FAR_FUTURE}">
<saml2:AudienceRestriction><saml2:Audience>{audience}</saml2:Audience></saml2:AudienceRestriction>
</saml2:Conditions>
<saml2:AuthnStatement AuthnInstant="2026-08-16T08:59:00.000Z" SessionIndex="id-okta-session-1" SessionNotOnOrAfter="{FAR_FUTURE}">
<saml2:AuthnContext><saml2:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml2:AuthnContextClassRef></saml2:AuthnContext>
</saml2:AuthnStatement>
<saml2:AttributeStatement>{attributes}</saml2:AttributeStatement>
</saml2:Assertion>
</saml2p:Response>"""


def entra_signed(**kwargs) -> str:
    """Entra signs the **Assertion**."""
    return fx.encode(fx.sign(entra_response_xml(**kwargs), target="assertion"))


def okta_signed(**kwargs) -> str:
    """Okta signs the **Response**."""
    return fx.encode(fx.sign(okta_response_xml(**kwargs), target="response"))
