"""Signed SAML fixtures — a throwaway IdP, generated in-process.

Golden captures from a real Entra or Okta tenant cannot be used here: they
expire, they carry a real tenant's identifiers, and a test that must edit one to
exercise a negative case has to re-sign it anyway. So the tests own a key pair
and mint their own assertions, which is also the only way to test the *positive*
path — an assertion that verifies.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner, methods

IDP_ENTITY_ID = "https://idp.test/entity"
SP_ENTITY_ID = "https://sp.test/entity"
#: Must name the tenant slug the endpoint tests post to: the ACS refuses a
#: request whose `{tenant}` segment is not the one its configured ACS URL names,
#: which is what stops an assertion being replayed into another tenant.
TENANT_SLUG = "saml-acme"
ACS_URL = f"https://sp.test/api/v1/auth/saml/{TENANT_SLUG}/acs"


def _keypair(common_name: str) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        cert.public_bytes(serialization.Encoding.PEM),
    )


#: The IdP's real signing key, and an unrelated one for "signed by the wrong key".
IDP_KEY, IDP_CERT = _keypair("idp.test")
ROGUE_KEY, ROGUE_CERT = _keypair("rogue.test")

FAR_FUTURE = "2099-01-01T00:00:00Z"
LONG_PAST = "2020-01-01T00:00:00Z"


def response_xml(
    *,
    response_id: str = "_resp1",
    assertion_id: str = "_assert1",
    issuer: str = IDP_ENTITY_ID,
    audience: str = SP_ENTITY_ID,
    destination: str = ACS_URL,
    recipient: str = ACS_URL,
    not_before: str = LONG_PAST,
    not_on_or_after: str = FAR_FUTURE,
    subject_expiry: str = FAR_FUTURE,
    name_id: str = "persistent-user-1",
    email: str | None = "someone@acme.com",
    display_name: str | None = "Some One",
    groups: tuple[str, ...] = ("Claims-Team",),
    status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    in_response_to: str | None = None,
) -> str:
    # Written on BOTH the Response element and the SubjectConfirmationData, as a
    # real IdP does — which is what lets a test prove we read the signed one.
    resp_irt = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    subj_irt = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    attrs = []
    if email:
        attrs.append(
            f'<saml:Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress">'
            f"<saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>"
        )
    if display_name:
        attrs.append(
            f'<saml:Attribute Name="displayName">'
            f"<saml:AttributeValue>{display_name}</saml:AttributeValue></saml:Attribute>"
        )
    for g in groups:
        attrs.append(
            f'<saml:Attribute Name="http://schemas.microsoft.com/ws/2008/06/identity/claims/groups">'
            f"<saml:AttributeValue>{g}</saml:AttributeValue></saml:Attribute>"
        )
    attr_block = (
        f"<saml:AttributeStatement>{''.join(attrs)}</saml:AttributeStatement>" if attrs else ""
    )
    return f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="{response_id}" Version="2.0" IssueInstant="2026-08-16T10:00:00Z" Destination="{destination}"{resp_irt}>
<saml:Issuer>{issuer}</saml:Issuer>
<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>
<saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="2026-08-16T10:00:00Z">
<saml:Issuer>{issuer}</saml:Issuer>
<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent">{name_id}</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
<saml:SubjectConfirmationData Recipient="{recipient}" NotOnOrAfter="{subject_expiry}"{subj_irt}/>
</saml:SubjectConfirmation></saml:Subject>
<saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_on_or_after}">
<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>
</saml:Conditions>
<saml:AuthnStatement SessionIndex="session-1" AuthnInstant="2026-08-16T10:00:00Z"/>
{attr_block}
</saml:Assertion>
</samlp:Response>"""


def sign(
    xml: str,
    *,
    key: bytes = IDP_KEY,
    cert: bytes = IDP_CERT,
    target: str = "response",
) -> bytes:
    """Sign the Response element, or the Assertion inside it. Returns XML bytes.

    Both are shapes real IdPs emit, and both have to verify — an SP that only
    handles one of them fails against half the market.
    """
    root = etree.fromstring(xml.encode())
    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
    )
    if target == "response":
        signed = signer.sign(root, key=key, cert=cert, reference_uri=root.get("ID"))
        return etree.tostring(signed)

    ns = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
    assertion = root.find("./saml:Assertion", ns)
    signed_assertion = signer.sign(
        assertion, key=key, cert=cert, reference_uri=assertion.get("ID"),
    )
    root.replace(assertion, signed_assertion)
    return etree.tostring(root)


def encode(xml: bytes | str) -> str:
    """Base64 as the IdP would POST it."""
    raw = xml.encode() if isinstance(xml, str) else xml
    return base64.b64encode(raw).decode()


def signed_response(**kwargs) -> str:
    """The happy path, base64-encoded."""
    target = kwargs.pop("target", "response")
    key = kwargs.pop("key", IDP_KEY)
    cert = kwargs.pop("cert", IDP_CERT)
    return encode(sign(response_xml(**kwargs), key=key, cert=cert, target=target))


def cert_body(pem: bytes = IDP_CERT) -> str:
    """The bare base64 blob an IdP admin console shows, without PEM armour."""
    text = pem.decode()
    inner = text.split("-----BEGIN CERTIFICATE-----")[1].split("-----END CERTIFICATE-----")[0]
    return "".join(inner.split())


# ── encryption ────────────────────────────────────────────────────────────
#
# An IdP that encrypts assertions seals a fresh symmetric key to the SP's public
# key and the assertion under that symmetric key. Reproduced here rather than
# mocked: decryption is the one part of this feature that cannot be proven by
# asserting on our own output, because the whole question is whether we can open
# what somebody *else* produced.

def encrypt_assertion(signed_xml: bytes, sp_cert_pem: str, *, mode: str = "cbc",
                      key_transport: str = "oaep") -> str:
    """Wrap the Assertion inside `signed_xml` as an EncryptedAssertion.

    `mode` picks AES-CBC (Entra and Okta's default) or AES-GCM.
    `key_transport` of "rsa15" produces the PKCS#1 v1.5 form the SP must refuse.
    """
    import base64 as _b64
    import os

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, padding as sym_padding
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    root = etree.fromstring(signed_xml)
    ns = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
    assertion = root.find("./saml:Assertion", ns)
    plaintext = etree.tostring(assertion)

    session_key = os.urandom(32)
    if mode == "gcm":
        iv = os.urandom(12)
        body = AESGCM(session_key).encrypt(iv, plaintext, None)
        data_alg = "http://www.w3.org/2009/xmlenc11#aes256-gcm"
    else:
        iv = os.urandom(16)
        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(session_key), modes.CBC(iv)).encryptor()
        body = encryptor.update(padded) + encryptor.finalize()
        data_alg = "http://www.w3.org/2001/04/xmlenc#aes256-cbc"

    public_key = x509.load_pem_x509_certificate(sp_cert_pem.encode()).public_key()
    if key_transport == "rsa15":
        wrapped = public_key.encrypt(session_key, asym_padding.PKCS1v15())
        key_alg = "http://www.w3.org/2001/04/xmlenc#rsa-1_5"
    else:
        wrapped = public_key.encrypt(
            session_key,
            asym_padding.OAEP(mgf=asym_padding.MGF1(hashes.SHA1()),
                              algorithm=hashes.SHA1(), label=None),
        )
        key_alg = "http://www.w3.org/2001/04/xmlenc#rsa-oaep-mgf1p"

    encrypted = f"""<saml:EncryptedAssertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
<xenc:EncryptedData xmlns:xenc="http://www.w3.org/2001/04/xmlenc#" Type="http://www.w3.org/2001/04/xmlenc#Element">
<xenc:EncryptionMethod Algorithm="{data_alg}"/>
<ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
<xenc:EncryptedKey><xenc:EncryptionMethod Algorithm="{key_alg}"/>
<xenc:CipherData><xenc:CipherValue>{_b64.b64encode(wrapped).decode()}</xenc:CipherValue></xenc:CipherData>
</xenc:EncryptedKey></ds:KeyInfo>
<xenc:CipherData><xenc:CipherValue>{_b64.b64encode(iv + body).decode()}</xenc:CipherValue></xenc:CipherData>
</xenc:EncryptedData></saml:EncryptedAssertion>"""

    root.replace(assertion, etree.fromstring(encrypted.encode()))
    return etree.tostring(root)
