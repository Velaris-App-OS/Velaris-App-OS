"""SAML 2.0 Service Provider — the protocol half, with no HTTP in it.

Everything here is pure: XML in, a verified :class:`SamlAssertion` or a
:class:`SamlError` out. The router
(:mod:`user_service.api.routers.auth_saml`) owns replay storage, provisioning
and token issuance, so the parts that are unforgiving to get wrong can be tested
against fixture assertions without a database or a live IdP.

**Library choice — signxml, not pysaml2.** The design called for pysaml2, whose
signature verification shells out to the ``xmlsec1`` binary; that would have put
an apt package in the critical path of a product installed by a shell script,
with a graceful-degradation branch to write and test for the case where it is
missing. ``signxml`` is pure Python over ``lxml`` and ``cryptography``, both of
which are already dependencies, so a base install can do SAML and there is no
"SAML is configured but the host is missing a binary" state to handle. The SAML
grammar we need is small and explicit; XML-DSig is the part worth handing to a
library, and that is exactly the part signxml is.

**The wrapping attack, and why the code reads the way it does.** XML Signature
Wrapping is the defect class that has broken most SAML implementations: the
attacker keeps a legitimately signed element in the document, adds a forged one,
and gets the verifier to check the first while the consumer reads the second.
The defence is not a check — it is never holding the unverified document in a
variable the consumer can reach. :func:`verify_response` therefore returns the
element tree that the signature *actually covered* (signxml's ``signed_xml``),
and every field is read from inside it. An assertion that is not the signed
element or a descendant of it is refused, so "signed" cannot come to mean
"appeared in a document that contained a signature".

Single Logout, encrypted assertions and SP-signed AuthnRequests all arrived
together in 2026-08, because they share one dependency: an SP key pair of our
own (:mod:`user_service.identity.saml_keys`). Encryption is *not* authenticity —
our certificate is published, so anyone can seal an assertion to it; see
:func:`_verified_encrypted_assertion` for the two shapes that are accepted.

Out of scope here, deliberately: artifact binding.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import textwrap
import time
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

from lxml import etree

log = logging.getLogger(__name__)

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml":  "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds":    "http://www.w3.org/2000/09/xmldsig#",
    "md":    "urn:oasis:names:tc:SAML:2.0:metadata",
}

#: A SAMLResponse is a form field, not a file upload. Anything larger than this
#: is refused **before** the parser sees it — an entity-expansion or
#: deeply-nested payload is cheap to send and expensive to parse, so the cap has
#: to come first to be worth anything.
MAX_RESPONSE_BYTES = 512 * 1024

#: How far the IdP's clock may differ from ours before a fresh assertion looks
#: expired. Overridable per provider; this is the value every IdP's own
#: documentation suggests.
DEFAULT_CLOCK_SKEW_SECONDS = 120

#: An upper bound on the configurable skew. Ten minutes is already generous for
#: a clock that is merely wrong; beyond it the value stops being tolerance and
#: starts being "accept expired assertions", which is what `NotOnOrAfter` exists
#: to prevent. Bounded here rather than at the admin API because `config` is a
#: plain JSON column a migration or a DBA can write directly.
MAX_CLOCK_SKEW_SECONDS = 600

STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"

NAMEID_PERSISTENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"
NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
NAMEID_UNSPECIFIED = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"

#: Entra ID and ADFS ship these claim URIs; Okta and Shibboleth use short names.
#: A provider's `attribute_map` overrides all of it — this is only what we try
#: when the admin has not said.
DEFAULT_ATTRIBUTE_MAP = {
    "email": [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "urn:oid:0.9.2342.19200300.100.1.3",
        "email", "mail", "emailAddress", "Email",
    ],
    "display_name": [
        "http://schemas.microsoft.com/identity/claims/displayname",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
        "urn:oid:2.16.840.1.113730.3.1.241",
        "displayName", "displayname", "name", "cn",
    ],
    # Okta's default SAML app sends `firstName` + `lastName` and NO display
    # name, so without these every Okta-provisioned user arrived nameless.
    # Used only to compose one when no display-name claim maps — see
    # `verify_response`.
    "given_name": [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
        "urn:oid:2.5.4.42",
        "firstName", "givenName", "given_name", "first_name",
    ],
    "family_name": [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
        "urn:oid:2.5.4.4",
        "lastName", "familyName", "family_name", "last_name", "surname", "sn",
    ],
    "groups": [
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
        "http://schemas.xmlsoap.org/claims/Group",
        "groups", "Groups", "memberOf",
    ],
    "manager": ["manager", "managerUserId", "http://schemas.xmlsoap.org/claims/Manager"],
}


def is_safe_local_path(value: str) -> bool:
    """A same-origin path, and nothing a browser will read as another host.

    ``//host`` is the obvious scheme-relative form, but a browser normalises
    ``\\`` to ``/`` in an http(s) URL and strips tab/newline before parsing, so
    ``/\\evil.com`` and ``/\\t/evil.com`` resolve off-origin too. Whitespace and
    control characters are refused outright rather than enumerated.
    """
    text = value or ""
    if not text.startswith("/") or text.startswith("//"):
        return False
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return False
    return "\\" not in text


def _safe_int(value: Any, default: int, *, low: int, high: int) -> int:
    """Bounded, never raising. `config` is JSON an admin or a migration writes."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


class SamlError(Exception):
    """A refusal, carrying a stable machine-readable reason.

    The reason is for logs and tests. It is deliberately **not** returned to the
    browser: "audience mismatch" and "signature invalid" together tell a forger
    which half of their attempt worked.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ── provider configuration ────────────────────────────────────────────────

@dataclass
class SamlConfig:
    """The IdP-specific half of an `sso_providers` row with `provider='saml'`.

    Lives in that row's existing `config` JSON — no new provider table, so
    enable/disable, tenant scoping and the admin CRUD all come for free.
    """

    idp_entity_id: str = ""
    idp_sso_url: str = ""
    idp_slo_url: str | None = None
    #: A LIST, so an IdP mid-rollover (two live signing certs) does not take the
    #: deployment down. Any listed cert verifying is enough.
    idp_x509_certs: list[str] = field(default_factory=list)
    sp_entity_id: str = ""
    #: Our ACS endpoint, configured rather than derived from the request.
    #: `Destination` and `Recipient` are checked against it, and deriving it from
    #: the `Host` header would make both checks compare an attacker-supplied
    #: value with itself. The admin has to paste this exact URL into the IdP
    #: anyway, so requiring it costs nothing.
    acs_url: str = ""
    name_id_format: str = NAMEID_PERSISTENT
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS
    attribute_map: dict[str, Any] = field(default_factory=dict)
    allowed_relay_state_prefixes: list[str] = field(default_factory=lambda: ["/studio"])
    auto_provision: bool = True
    email_domain_allowlist: list[str] = field(default_factory=list)
    default_roles: list[str] = field(default_factory=list)
    #: May a login adopt an existing Velaris account that matches only on the
    #: asserted email address? **Default false, and it should stay false.**
    #: With it on, whoever can set an email attribute in the directory signs in
    #: as whichever account already holds that address, inheriting its roles —
    #: the corporate domain is exactly what `email_domain_allowlist` allows, so
    #: that gate does not bite. Off, an unrecognised `external_id` provisions a
    #: new account and a taken address is refused for an administrator to link.
    link_existing_by_email: bool = False
    #: Where the IdP publishes its federation metadata. Optional, and never the
    #: source of truth at sign-in time — the fields above are. It exists so the
    #: admin screen can fill those fields in one paste, and so a scheduled
    #: refresh can add a rollover certificate before the old one expires.
    idp_metadata_url: str = ""
    #: Sign the AuthnRequest we send. Needs an SP key pair; off by default
    #: because an unsigned request is valid SAML and every IdP tested accepts
    #: one, so defaulting it on would break existing providers at the first
    #: restart.
    sign_authn_requests: bool = False
    #: Offer single logout. Also needs the key pair — a LogoutRequest is only
    #: honoured signed.
    slo_enabled: bool = False

    @classmethod
    def from_row(cls, config: dict | None) -> "SamlConfig":
        cfg = dict(config or {})
        certs = [str(c).strip() for c in (cfg.get("idp_x509_certs") or []) if str(c).strip()]
        return cls(
            idp_entity_id=str(cfg.get("idp_entity_id") or "").strip(),
            idp_sso_url=str(cfg.get("idp_sso_url") or "").strip(),
            idp_slo_url=(str(cfg.get("idp_slo_url")).strip() or None) if cfg.get("idp_slo_url") else None,
            idp_x509_certs=certs,
            sp_entity_id=str(cfg.get("sp_entity_id") or "").strip(),
            acs_url=str(cfg.get("acs_url") or "").strip(),
            name_id_format=str(cfg.get("name_id_format") or NAMEID_PERSISTENT),
            clock_skew_seconds=_safe_int(
                cfg.get("clock_skew_seconds") or DEFAULT_CLOCK_SKEW_SECONDS,
                DEFAULT_CLOCK_SKEW_SECONDS, low=0, high=MAX_CLOCK_SKEW_SECONDS,
            ),
            attribute_map=dict(cfg.get("attribute_map") or {}),
            # Sanitised on the way in, so no caller has to remember to check.
            # `relay_state_allowed` matches against this list, and the ACS uses
            # its first entry as the landing page when the IdP sends no
            # RelayState at all — a `//evil.com` prefix would put an
            # attacker-chosen host in a `Location` that carries a live handoff
            # code, on the one redirect that is not checked because it never
            # came from outside.
            allowed_relay_state_prefixes=[
                str(p) for p in (cfg.get("allowed_relay_state_prefixes") or ["/studio"])
                if is_safe_local_path(str(p))
            ],
            auto_provision=bool(cfg.get("auto_provision", True)),
            email_domain_allowlist=[
                str(d).strip().lower().lstrip("@")
                for d in (cfg.get("email_domain_allowlist") or []) if str(d).strip()
            ],
            default_roles=[str(r) for r in (cfg.get("default_roles") or [])],
            link_existing_by_email=bool(cfg.get("link_existing_by_email", False)),
            idp_metadata_url=str(cfg.get("idp_metadata_url") or "").strip(),
            sign_authn_requests=bool(cfg.get("sign_authn_requests", False)),
            slo_enabled=bool(cfg.get("slo_enabled", False)),
        )

    def missing(self) -> list[str]:
        """Which required fields are absent. A provider missing any is unusable."""
        required = {
            "idp_entity_id": self.idp_entity_id,
            "idp_sso_url": self.idp_sso_url,
            "sp_entity_id": self.sp_entity_id,
            "acs_url": self.acs_url,
        }
        gaps = [k for k, v in required.items() if not v]
        if not self.idp_x509_certs:
            gaps.append("idp_x509_certs")
        return gaps


# ── XML parsing ───────────────────────────────────────────────────────────

def hardened_parser() -> etree.XMLParser:
    """A parser that cannot be talked into fetching or expanding anything.

    ``resolve_entities=False`` closes XXE and billion-laughs; ``no_network``
    stops a DTD or schema reference becoming an outbound request from the login
    path; ``huge_tree=False`` keeps libxml2's own depth and size limits on.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        recover=False,
    )


def _decode_response(saml_response: str) -> bytes:
    """base64 → XML bytes, with the size cap applied on both sides.

    The encoded form is checked first so an oversized payload is dropped before
    it is even decoded, and the decoded form second so a compressible payload
    cannot slip past the first check.
    """
    raw = (saml_response or "").strip()
    if not raw:
        raise SamlError("empty_response")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SamlError("response_too_large", f"{len(raw)} encoded bytes")
    try:
        xml = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SamlError("malformed_base64", str(exc)) from exc
    if len(xml) > MAX_RESPONSE_BYTES:
        raise SamlError("response_too_large", f"{len(xml)} decoded bytes")
    return xml


def _pem(cert: str) -> str:
    """Accept a bare base64 blob (what every IdP UI shows) or a full PEM."""
    body = (cert or "").strip()
    if "BEGIN CERTIFICATE" in body:
        return body
    body = "".join(body.split())
    wrapped = "\n".join(textwrap.wrap(body, 64))
    return f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----\n"


# ── time ──────────────────────────────────────────────────────────────────

def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── the verified result ───────────────────────────────────────────────────

@dataclass
class SamlAssertion:
    assertion_id: str
    issuer: str
    name_id: str
    name_id_format: str | None
    not_on_or_after: datetime | None
    session_index: str | None
    attributes: dict[str, list[str]]

    email: str | None = None
    display_name: str | None = None
    groups: list[str] = field(default_factory=list)
    manager: str | None = None
    #: The AuthnRequest this answers, taken from the SIGNED subject data. None
    #: for an IdP-initiated login, which answers no request at all.
    in_response_to: str | None = None


def _mapped(cfg: SamlConfig, key: str, attributes: dict[str, list[str]]) -> list[str]:
    """Resolve one logical field through the provider's map, then the defaults."""
    configured = cfg.attribute_map.get(key)
    candidates: list[str] = []
    if isinstance(configured, str) and configured.strip():
        candidates.append(configured.strip())
    elif isinstance(configured, (list, tuple)):
        candidates.extend(str(c) for c in configured if str(c).strip())
    candidates.extend(DEFAULT_ATTRIBUTE_MAP.get(key, []))

    for name in candidates:
        if name in attributes and attributes[name]:
            return attributes[name]
    return []


def verify_response(
    saml_response: str,
    *,
    cfg: SamlConfig,
    acs_url: str,
    now: datetime | None = None,
    private_key_pem: str | None = None,
) -> SamlAssertion:
    """Verify a SAMLResponse and return the assertion it actually vouches for.

    Refuses on the first failure with a :class:`SamlError`. The order matters:
    cheap structural checks run before the signature so a flood of junk is not a
    crypto workload, and **nothing is read out of the document until the
    signature has told us which subtree to read it from**.
    """
    now = now or datetime.now(timezone.utc)
    skew = timedelta(seconds=max(0, cfg.clock_skew_seconds))

    gaps = cfg.missing()
    if gaps:
        raise SamlError("provider_incomplete", ",".join(gaps))

    xml = _decode_response(saml_response)
    parser = hardened_parser()
    try:
        document = etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SamlError("malformed_xml", str(exc)) from exc

    if etree.QName(document).localname != "Response":
        raise SamlError("not_a_response", etree.QName(document).localname)

    # Status is read from the unsigned document only to fail fast on an IdP that
    # is telling us "no". A forged success status buys nothing: the assertion
    # below still has to verify, and the whole result comes from the signed
    # subtree.
    status = document.find("./samlp:Status/samlp:StatusCode", NS)
    if status is not None and (status.get("Value") or "") != STATUS_SUCCESS:
        raise SamlError("idp_refused", status.get("Value") or "")

    from user_service.identity import saml_encryption

    if saml_encryption.has_encrypted_assertion(document):
        signed_root, assertion = _verified_encrypted_assertion(
            xml, cfg=cfg, parser=parser, private_key_pem=private_key_pem)
    else:
        signed_root = _verified_subtree(xml, cfg=cfg, parser=parser)
        # ── from here down, only `signed_root` is read ────────────────────
        assertion = _assertion_within(signed_root)

    issuer_el = assertion.find("./saml:Issuer", NS)
    issuer = (issuer_el.text or "").strip() if issuer_el is not None else ""
    if issuer != cfg.idp_entity_id:
        raise SamlError("issuer_mismatch", issuer)

    assertion_id = (assertion.get("ID") or "").strip()
    if not assertion_id:
        raise SamlError("assertion_id_missing")

    # Destination is checked when present and is **not** what binds the
    # assertion to this endpoint — `Recipient` is (see `_check_subject`). When
    # only the Assertion is signed, which is the Entra and Okta default, the
    # signed subtree has no Destination and this falls back to the UNSIGNED
    # document: a forger deletes the attribute rather than matching it, so the
    # check can refuse a mistake but cannot stop an attacker. It is kept for the
    # response-signed case, where it is covered by the signature.
    destination = (signed_root.get("Destination") or document.get("Destination") or "").strip()
    if destination and destination.rstrip("/") != acs_url.rstrip("/"):
        raise SamlError("destination_mismatch", destination)

    _check_audience(assertion, cfg)
    _check_conditions(assertion, now=now, skew=skew)
    subject_expiry, in_response_to = _check_subject(
        assertion, acs_url=acs_url, now=now, skew=skew)

    name_id_el = assertion.find("./saml:Subject/saml:NameID", NS)
    name_id = (name_id_el.text or "").strip() if name_id_el is not None else ""
    if not name_id:
        raise SamlError("name_id_missing")

    attributes = _attributes(assertion)
    statement = assertion.find("./saml:AuthnStatement", NS)

    result = SamlAssertion(
        assertion_id=assertion_id,
        issuer=issuer,
        name_id=name_id,
        name_id_format=name_id_el.get("Format") if name_id_el is not None else None,
        not_on_or_after=subject_expiry,
        session_index=statement.get("SessionIndex") if statement is not None else None,
        attributes=attributes,
        in_response_to=in_response_to,
    )

    emails = _mapped(cfg, "email", attributes)
    if not emails and "@" in name_id:
        # An emailAddress NameID is the address, and several IdPs send no
        # separate claim when that is the format in use.
        emails = [name_id]
    result.email = emails[0].strip().lower() if emails else None

    names = _mapped(cfg, "display_name", attributes)
    if names:
        result.display_name = names[0].strip()
    else:
        # Okta's default SAML app sends `firstName` + `lastName` and no display
        # name at all, so every Okta-provisioned user used to arrive nameless.
        # Composed the same way `scim.display_name` composes a SCIM `name`
        # object, rather than inventing a second rule for the same problem.
        given = _mapped(cfg, "given_name", attributes)
        family = _mapped(cfg, "family_name", attributes)
        joined = " ".join(p for p in (
            given[0].strip() if given else "",
            family[0].strip() if family else "",
        ) if p)
        result.display_name = joined or None
    result.groups = [g for g in _mapped(cfg, "groups", attributes) if g.strip()]
    managers = _mapped(cfg, "manager", attributes)
    result.manager = managers[0].strip() if managers else None

    return result


def _verified_subtree(
    xml: bytes, *, cfg: SamlConfig, parser: etree.XMLParser,
):
    """Return the element the IdP's signature actually covers.

    Every listed certificate is tried, because an IdP mid-rollover has two live
    signing keys and pinning one would turn a routine rotation into an outage.
    The failure is uniform: which certificate failed, and how, is not something
    a caller gets to learn one attempt at a time.
    """
    from signxml import SignatureConfiguration, XMLVerifier

    if not cfg.idp_x509_certs:
        raise SamlError("no_idp_certificate")

    expect = SignatureConfiguration(require_x509=True, location=".//", expect_references=1)
    last: Exception | None = None
    for cert in cfg.idp_x509_certs:
        try:
            verified = XMLVerifier().verify(
                xml,
                x509_cert=_pem(cert),
                parser=parser,
                expect_config=expect,
            )
        except Exception as exc:            # signxml raises a family of these
            last = exc
            continue
        signed = getattr(verified, "signed_xml", None)
        if signed is None:
            last = ValueError("verifier returned no signed element")
            continue
        return signed

    raise SamlError("signature_invalid", str(last) if last else "")


def _verified_encrypted_assertion(xml: bytes, *, cfg: SamlConfig, parser, private_key_pem):
    """Open an `EncryptedAssertion` **and** establish that a signature covers it.

    Encryption is confidentiality, not authenticity: anyone holding our public
    key can produce a well-formed `EncryptedAssertion`. So decryption alone
    proves nothing, and there are exactly two shapes that do:

    **A — the Response is signed.** The signature covers the ciphertext, so the
    plaintext inside it is vouched for. The element decrypted is taken from the
    *signed subtree*, never from the raw document, or an attacker could sign one
    Response and have us decrypt an `EncryptedAssertion` they appended.

    **B — the Response is unsigned and the assertion inside was signed before
    being encrypted.** Decrypt first, then require a signature on the result.

    Anything else — an encrypted assertion with no signature anywhere — is
    refused. Accepting it would let anyone with our published certificate mint a
    login.
    """
    from user_service.identity import saml_encryption

    if not private_key_pem:
        # Only reachable when the IdP encrypts but this provider has no key
        # pair. Logged, because it is a setup mistake an admin must fix and no
        # amount of retrying will clear it.
        log.warning("saml: an encrypted assertion arrived but this provider has "
                    "no SP key pair configured")
        raise SamlError("encrypted_assertion")

    def _open(element):
        try:
            return saml_encryption.decrypt_assertion(element, private_key_pem, parser=parser)
        except saml_encryption.DecryptionError as exc:
            raise SamlError("encrypted_assertion") from exc

    try:
        signed_root = _verified_subtree(xml, cfg=cfg, parser=parser)
    except SamlError:
        signed_root = None

    if signed_root is not None and etree.QName(signed_root).localname == "Response":
        encrypted = signed_root.find("./saml:EncryptedAssertion", NS)
        if encrypted is None:
            raise SamlError("assertion_count", "0")
        return signed_root, _open(encrypted)

    # Shape B. Re-parse the raw document only to locate the ciphertext; the
    # signature check below is what makes the plaintext trustworthy.
    document = etree.fromstring(xml, parser=hardened_parser())
    encrypted = document.find("./saml:EncryptedAssertion", NS)
    if encrypted is None:
        raise SamlError("assertion_count", "0")
    plain = _open(encrypted)

    inner_signed = _verified_subtree(
        etree.tostring(plain), cfg=cfg, parser=hardened_parser())
    if etree.QName(inner_signed).localname != "Assertion":
        raise SamlError("unexpected_signed_element", etree.QName(inner_signed).localname)
    return inner_signed, inner_signed


def _assertion_within(signed_root):
    """The assertion, taken from inside the verified subtree and nowhere else.

    Two shapes are legitimate: the IdP signed the Response (the assertion is a
    child of it) or signed the Assertion itself. Both are accepted, and both
    yield an assertion the signature covers. An unsigned assertion sitting
    beside a signed one is simply never reachable from here — which is the
    property, rather than a check that could be forgotten.
    """
    name = etree.QName(signed_root).localname
    if name == "Assertion":
        return signed_root
    if name == "Response":
        found = signed_root.findall("./saml:Assertion", NS)
        if len(found) != 1:
            # Zero: the signature covered a Response carrying nothing usable.
            # More than one: which identity the IdP meant is genuinely ambiguous,
            # and picking the first is how implementations get walked into
            # accepting an attacker's.
            raise SamlError("assertion_count", str(len(found)))
        return found[0]
    raise SamlError("unexpected_signed_element", name)


def _check_audience(assertion, cfg: SamlConfig) -> None:
    """Audience confusion: an assertion minted for another SP must not log in here."""
    audiences = [
        (el.text or "").strip()
        for el in assertion.findall(
            "./saml:Conditions/saml:AudienceRestriction/saml:Audience", NS)
    ]
    if not audiences:
        raise SamlError("audience_missing")
    if cfg.sp_entity_id not in audiences:
        raise SamlError("audience_mismatch", ",".join(audiences))


def _check_conditions(assertion, *, now: datetime, skew: timedelta) -> None:
    conditions = assertion.find("./saml:Conditions", NS)
    if conditions is None:
        raise SamlError("conditions_missing")
    not_before = _parse_instant(conditions.get("NotBefore"))
    not_after = _parse_instant(conditions.get("NotOnOrAfter"))
    if not_before is not None and now + skew < not_before:
        raise SamlError("not_yet_valid", conditions.get("NotBefore") or "")
    if not_after is not None and now - skew >= not_after:
        raise SamlError("expired", conditions.get("NotOnOrAfter") or "")


def _check_subject(
    assertion, *, acs_url: str, now: datetime, skew: timedelta,
) -> tuple[datetime | None, str | None]:
    """SubjectConfirmation — the half that binds the assertion to this delivery.

    ``Recipient`` is the check that stops an assertion captured at one SP being
    replayed at another, and it is separate from ``Audience``: one names who the
    assertion is *about being for*, the other where it was *sent*.

    **It is required, not checked-when-present.** The Web Browser SSO profile
    says a bearer ``SubjectConfirmationData`` MUST carry a ``Recipient`` and the
    SP MUST verify it — so treating an absent one as "nothing to check" made the
    only binding between an assertion and this endpoint optional at the
    *sender's* discretion. That matters more here than it looks: with an
    assertion-signed Response (the Entra **and** Okta default) ``Destination``
    lives on the unsigned document, so it is not a second line of defence —
    a forger deletes it rather than matching it. Every IdP we support sends
    ``Recipient``; one that does not is a misconfiguration worth failing loudly,
    and `saml_test` names the reason for whoever has to fix it.

    Not narrowed here, deliberately: ``Method`` is not required to be
    ``bearer``, and an assertion carrying several ``SubjectConfirmation``
    elements is read from the first. Both are pre-existing and neither is
    reachable from an IdP we support.
    """
    data = assertion.find(
        "./saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData", NS)
    if data is None:
        raise SamlError("subject_confirmation_missing")

    recipient = (data.get("Recipient") or "").strip()
    if not recipient:
        raise SamlError("recipient_missing")
    if recipient.rstrip("/") != acs_url.rstrip("/"):
        raise SamlError("recipient_mismatch", recipient)

    not_after = _parse_instant(data.get("NotOnOrAfter"))
    if not_after is not None and now - skew >= not_after:
        raise SamlError("subject_expired", data.get("NotOnOrAfter") or "")

    # Read from the *signed* subject data, never from the Response element: with
    # an assertion-signed Response the Response attribute is unsigned, so a
    # forger would simply write whichever value they liked. Absent is normal —
    # an IdP-initiated login answers no request — so the caller decides what to
    # do with it rather than this function refusing.
    return not_after, (data.get("InResponseTo") or "").strip() or None


def _attributes(assertion) -> dict[str, list[str]]:
    """Every asserted attribute, keyed by both Name and FriendlyName.

    Indexing under both is what lets one `attribute_map` work across IdPs that
    send `http://schemas.../emailaddress` and ones that send `mail`.
    """
    out: dict[str, list[str]] = {}
    for attr in assertion.findall("./saml:AttributeStatement/saml:Attribute", NS):
        values = [
            (v.text or "").strip()
            for v in attr.findall("./saml:AttributeValue", NS)
            if (v.text or "").strip()
        ]
        if not values:
            continue
        for key in (attr.get("Name"), attr.get("FriendlyName")):
            if key:
                out.setdefault(key, []).extend(values)
    return out


# ── outbound: metadata and AuthnRequest ───────────────────────────────────

def build_metadata(
    *, sp_entity_id: str, acs_url: str, name_id_format: str = NAMEID_PERSISTENT,
    sp_cert: str | None = None, sign_authn_requests: bool = False,
    slo_url: str | None = None,
) -> bytes:
    """SP metadata for the IdP administrator to upload.

    **A certificate is advertised only when one exists.** Publishing a key we
    cannot use invites the IdP to start encrypting assertions to it, and every
    login then fails in a way that looks like a signature problem. With a key
    present, both uses are declared — signing (AuthnRequests, LogoutRequests)
    and encryption (assertions sealed to us) — because they are the same pair.
    """
    root = etree.Element(
        f"{{{NS['md']}}}EntityDescriptor",
        nsmap={"md": NS["md"], "ds": NS["ds"]},
        entityID=sp_entity_id,
    )
    sp = etree.SubElement(
        root, f"{{{NS['md']}}}SPSSODescriptor",
        AuthnRequestsSigned="true" if (sign_authn_requests and sp_cert) else "false",
        WantAssertionsSigned="true",
        protocolSupportEnumeration=NS["samlp"],
    )

    if sp_cert:
        from user_service.identity.saml_keys import cert_body

        body = cert_body(sp_cert)
        for use in ("signing", "encryption"):
            kd = etree.SubElement(sp, f"{{{NS['md']}}}KeyDescriptor", use=use)
            info = etree.SubElement(kd, f"{{{NS['ds']}}}KeyInfo")
            data = etree.SubElement(info, f"{{{NS['ds']}}}X509Data")
            etree.SubElement(data, f"{{{NS['ds']}}}X509Certificate").text = body

    if slo_url:
        etree.SubElement(
            sp, f"{{{NS['md']}}}SingleLogoutService",
            Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            Location=slo_url,
        )

    fmt = etree.SubElement(sp, f"{{{NS['md']}}}NameIDFormat")
    fmt.text = name_id_format
    etree.SubElement(
        sp, f"{{{NS['md']}}}AssertionConsumerService",
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
        Location=acs_url, index="0", isDefault="true",
    )
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


#: XML-DSIG identifiers for the HTTP-Redirect signature. RSA-SHA256 only —
#: SHA-1 is still what several IdP consoles offer by default and is not
#: acceptable for a signature anyone relies on.
SIG_ALG_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"


def sign_redirect_query(query: str, private_key_pem: str) -> str:
    """The `Signature` value for an HTTP-Redirect binding message.

    Redirect binding does **not** sign the XML — it signs the URL-encoded query
    string, in a fixed parameter order, precisely because DEFLATE+base64 is not
    canonical and two encoders would otherwise disagree about what was signed.
    The caller must build `query` in the order the specification requires
    (`SAMLRequest`/`SAMLResponse`, then `RelayState` if present, then `SigAlg`)
    and send those parameters in that same order.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(query.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def build_authn_request(
    *, cfg: SamlConfig, acs_url: str, relay_state: str | None = None,
    private_key_pem: str | None = None,
) -> tuple[str, str]:
    """Build a redirect-binding AuthnRequest. Returns ``(url, request_id)``.

    Signed when `private_key_pem` is supplied — several IdPs require it, and
    most enterprise security reviews ask for it. Unsigned otherwise, which is
    valid SAML and what every IdP we have tested accepts by default.

    The ID is returned so the caller can record it; an IdP that echoes
    ``InResponseTo`` gives a second, cheaper replay signal than the assertion-id
    table, but SP-initiated state is not what makes the flow safe — the
    signature and the replay table are.
    """
    request_id = f"_{uuid.uuid4().hex}"
    root = etree.Element(
        f"{{{NS['samlp']}}}AuthnRequest",
        nsmap={"samlp": NS["samlp"], "saml": NS["saml"]},
        ID=request_id,
        Version="2.0",
        IssueInstant=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        Destination=cfg.idp_sso_url,
        ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
        AssertionConsumerServiceURL=acs_url,
    )
    issuer = etree.SubElement(root, f"{{{NS['saml']}}}Issuer")
    issuer.text = cfg.sp_entity_id
    etree.SubElement(
        root, f"{{{NS['samlp']}}}NameIDPolicy",
        Format=cfg.name_id_format, AllowCreate="true",
    )

    xml = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
    # HTTP-Redirect binding: raw DEFLATE (no zlib header), then base64, then
    # URL-encode. The negative window size is what strips the header.
    deflated = zlib.compress(xml)[2:-4]
    params = {"SAMLRequest": base64.b64encode(deflated).decode()}
    if relay_state:
        params["RelayState"] = relay_state

    if private_key_pem:
        # Parameter order is part of the signature, not a style choice: the
        # verifier reconstructs this exact string from the query it received.
        params["SigAlg"] = SIG_ALG_RSA_SHA256
        signed_part = urlencode(params, quote_via=quote)
        params["Signature"] = sign_redirect_query(signed_part, private_key_pem)

    sep = "&" if "?" in cfg.idp_sso_url else "?"
    return f"{cfg.idp_sso_url}{sep}{urlencode(params, quote_via=quote)}", request_id


# ── single logout ─────────────────────────────────────────────────────────
#
# Logout is the mirror of login and needs the same suspicion. A `LogoutRequest`
# arrives on a public endpoint claiming "end this person's session"; unverified,
# it is a denial-of-service anyone can aim at any user by name. So the IdP's
# signature is checked before anything is revoked — and because redirect binding
# signs the *query string* rather than the XML, verification has to work from
# the raw query, in the order the specification fixes.

def _signed_query(raw_query: str, message_param: str) -> str | None:
    """Reconstruct exactly what the IdP signed, from the raw query string.

    Re-encoding the parsed values would not do: percent-encoding is not
    canonical, so a value that round-trips differently produces a different
    string and a signature that "fails" for no reason. The raw substrings are
    taken in the fixed order: message, then RelayState if present, then SigAlg.
    """
    parts = {}
    for chunk in (raw_query or "").split("&"):
        name, _, value = chunk.partition("=")
        if name and name not in parts:
            parts[name] = value
    if message_param not in parts or "SigAlg" not in parts:
        return None
    ordered = [f"{message_param}={parts[message_param]}"]
    if "RelayState" in parts:
        ordered.append(f"RelayState={parts['RelayState']}")
    ordered.append(f"SigAlg={parts['SigAlg']}")
    return "&".join(ordered)


def verify_redirect_signature(raw_query: str, message_param: str, cfg: SamlConfig) -> bool:
    """Is this redirect-binding message signed by the configured IdP?

    Every certificate is tried, for the same reason the assertion path tries
    them all: an IdP mid-rollover has two live keys. Never raises — an
    unparseable signature is simply not a valid one.
    """
    from urllib.parse import unquote

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    signed = _signed_query(raw_query, message_param)
    if signed is None:
        return False

    params = dict(
        (c.partition("=")[0], c.partition("=")[2]) for c in (raw_query or "").split("&") if c
    )
    if unquote(params.get("SigAlg", "")) != SIG_ALG_RSA_SHA256:
        return False
    try:
        signature = base64.b64decode(unquote(params.get("Signature", "")), validate=True)
    except (binascii.Error, ValueError):
        return False

    for cert in cfg.idp_x509_certs:
        try:
            public_key = serialization.load_pem_public_key(
                _certificate_public_pem(cert).encode())
            public_key.verify(signature, signed.encode(), padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:
            continue
    return False


def _certificate_public_pem(cert: str) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    loaded = x509.load_pem_x509_certificate(_pem(cert).encode())
    return loaded.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


@dataclass
class LogoutRequest:
    request_id: str
    issuer: str
    name_id: str
    session_index: str | None


def parse_logout_request(saml_request: str) -> LogoutRequest:
    """Decode and read an IdP-initiated `LogoutRequest`. Structure only.

    Whether to *act* on it is the caller's decision, made after
    `verify_redirect_signature` — parsing an unsigned request is harmless,
    honouring one is not.
    """
    xml = _inflate(saml_request)
    try:
        root = etree.fromstring(xml, parser=hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise SamlError("malformed_xml", str(exc)) from exc
    if etree.QName(root).localname != "LogoutRequest":
        raise SamlError("not_a_logout_request", etree.QName(root).localname)

    issuer_el = root.find("./saml:Issuer", NS)
    name_el = root.find("./saml:NameID", NS)
    idx_el = root.find("./samlp:SessionIndex", NS)
    return LogoutRequest(
        request_id=(root.get("ID") or "").strip(),
        issuer=((issuer_el.text or "").strip() if issuer_el is not None else ""),
        name_id=((name_el.text or "").strip() if name_el is not None else ""),
        session_index=((idx_el.text or "").strip() if idx_el is not None else None),
    )


def _inflate(message: str) -> bytes:
    """base64 → raw-DEFLATE → XML, with the size cap on both sides."""
    raw = (message or "").strip()
    if not raw:
        raise SamlError("empty_response")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SamlError("response_too_large", f"{len(raw)} encoded bytes")
    try:
        packed = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SamlError("malformed_base64", str(exc)) from exc
    try:
        xml = zlib.decompressobj(-15).decompress(packed, MAX_RESPONSE_BYTES + 1)
    except zlib.error:
        xml = packed                     # some IdPs POST it undeflated
    if len(xml) > MAX_RESPONSE_BYTES:
        raise SamlError("response_too_large", f"{len(xml)} decoded bytes")
    return xml


def _redirect_url(destination: str, params: dict, private_key_pem: str | None,
                  message_param: str) -> str:
    if private_key_pem:
        params["SigAlg"] = SIG_ALG_RSA_SHA256
        params["Signature"] = sign_redirect_query(
            urlencode(params, quote_via=quote), private_key_pem)
    sep = "&" if "?" in destination else "?"
    return f"{destination}{sep}{urlencode(params, quote_via=quote)}"


def build_logout_request(
    *, cfg: SamlConfig, name_id: str, session_index: str | None = None,
    private_key_pem: str | None = None, relay_state: str | None = None,
) -> tuple[str, str]:
    """SP-initiated logout: a `LogoutRequest` to the IdP. ``(url, request_id)``."""
    if not cfg.idp_slo_url:
        raise SamlError("no_idp_slo_url")
    request_id = f"_{uuid.uuid4().hex}"
    root = etree.Element(
        f"{{{NS['samlp']}}}LogoutRequest",
        nsmap={"samlp": NS["samlp"], "saml": NS["saml"]},
        ID=request_id, Version="2.0",
        IssueInstant=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        Destination=cfg.idp_slo_url,
    )
    etree.SubElement(root, f"{{{NS['saml']}}}Issuer").text = cfg.sp_entity_id
    name = etree.SubElement(root, f"{{{NS['saml']}}}NameID", Format=cfg.name_id_format)
    name.text = name_id
    if session_index:
        etree.SubElement(root, f"{{{NS['samlp']}}}SessionIndex").text = session_index

    deflated = zlib.compress(etree.tostring(root, encoding="UTF-8"))[2:-4]
    params = {"SAMLRequest": base64.b64encode(deflated).decode()}
    if relay_state:
        params["RelayState"] = relay_state
    return _redirect_url(cfg.idp_slo_url, params, private_key_pem, "SAMLRequest"), request_id


def build_logout_response(
    *, cfg: SamlConfig, in_response_to: str, private_key_pem: str | None = None,
    relay_state: str | None = None,
) -> str:
    """Answer an IdP-initiated `LogoutRequest` with a Success `LogoutResponse`."""
    if not cfg.idp_slo_url:
        raise SamlError("no_idp_slo_url")
    root = etree.Element(
        f"{{{NS['samlp']}}}LogoutResponse",
        nsmap={"samlp": NS["samlp"], "saml": NS["saml"]},
        ID=f"_{uuid.uuid4().hex}", Version="2.0",
        IssueInstant=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        Destination=cfg.idp_slo_url, InResponseTo=in_response_to,
    )
    etree.SubElement(root, f"{{{NS['saml']}}}Issuer").text = cfg.sp_entity_id
    status = etree.SubElement(root, f"{{{NS['samlp']}}}Status")
    etree.SubElement(status, f"{{{NS['samlp']}}}StatusCode", Value=STATUS_SUCCESS)

    deflated = zlib.compress(etree.tostring(root, encoding="UTF-8"))[2:-4]
    params = {"SAMLResponse": base64.b64encode(deflated).decode()}
    if relay_state:
        params["RelayState"] = relay_state
    return _redirect_url(cfg.idp_slo_url, params, private_key_pem, "SAMLResponse")


# ── RelayState ────────────────────────────────────────────────────────────
#
# RelayState is echoed back by the IdP verbatim and lands in a 302 `Location`,
# so an unchecked value is an open redirect on an endpoint that has just issued
# a session. Two independent controls, because either alone has a gap: the
# signature proves we minted it (an attacker cannot point it anywhere), and the
# prefix allowlist bounds it (a value we minted for one surface cannot be
# steered to another). It expires because a login link should not stay live.

_RELAY_VERSION = "v1"
_RELAY_PURPOSE = b"velaris.saml-relay-state.v1"
RELAY_TTL_SECONDS = 15 * 60


def _relay_key() -> bytes:
    from velaris_shared.config import get_settings

    return hmac.new(
        (get_settings().auth_secret or "").encode(), _RELAY_PURPOSE, hashlib.sha256,
    ).digest()


def _relay_sig(target: str, expires_at: int) -> str:
    payload = f"{_RELAY_VERSION}.{target}.{expires_at}".encode()
    return base64.urlsafe_b64encode(
        hmac.new(_relay_key(), payload, hashlib.sha256).digest()
    ).decode().rstrip("=")


def relay_state_allowed(target: str, cfg: SamlConfig) -> bool:
    """A relative path under one of the configured prefixes, and nothing else.

    Refusing anything a browser would resolve against another host is what keeps
    this from being an open redirect; the prefix list is what keeps a valid login
    from landing somewhere the admin never intended.

    **`\\n`, `\\r` and `\\` were not the whole set.** A browser strips *every*
    C0 control from a URL before parsing it, so ``/\\t/evil.test`` — which has no
    backslash, does not start with ``//`` and matches a ``/`` prefix — becomes
    ``//evil.test``. That was live: `/login?target=…` mints a signed RelayState
    for any target it accepts, so an attacker could hand a victim a link whose
    completed sign-in redirected to their host **carrying the handoff code**.
    `is_safe_local_path` refuses the whole class rather than the three examples
    of it, and is the same rule the prefixes and `icon_url` are held to.
    """
    value = (target or "").strip()
    if not is_safe_local_path(value):
        return False
    return any(value.startswith(p) for p in (cfg.allowed_relay_state_prefixes or []))


def default_landing(cfg: SamlConfig) -> str:
    """Where a sign-in lands when the IdP sends no usable RelayState.

    The first configured prefix is the administrator's landing page. It reaches
    a `Location` header without ever having been through `relay_state_allowed`
    — nothing arrived from outside, so nothing looked at it — which is why the
    safety of that value cannot rest on the prefix list alone.
    """
    for prefix in cfg.allowed_relay_state_prefixes or []:
        if is_safe_local_path(prefix):
            return prefix
    return "/"


def mint_relay_state(target: str, *, ttl_seconds: int = RELAY_TTL_SECONDS) -> str:
    expires_at = int(time.time()) + ttl_seconds
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return f"{_RELAY_VERSION}.{encoded}.{expires_at}.{_relay_sig(target, expires_at)}"


def verify_relay_state(value: str, cfg: SamlConfig) -> str | None:
    """The target this RelayState carries, or None. Never raises."""
    try:
        version, encoded, expiry, sig = (value or "").split(".", 3)
        if version != _RELAY_VERSION:
            return None
        expires_at = int(expiry)
        if expires_at < int(time.time()):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        target = base64.urlsafe_b64decode(padded.encode()).decode()
        if not secrets.compare_digest(sig, _relay_sig(target, expires_at)):
            return None
    except Exception:
        return None
    return target if relay_state_allowed(target, cfg) else None
