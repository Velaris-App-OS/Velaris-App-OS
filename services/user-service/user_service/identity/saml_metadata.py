"""SAML 2.0 IdP metadata — parse, import, and keep signing certificates fresh.

Setting up a SAML provider means copying three values out of the identity
provider's console, and every product names them differently: what we call the
IdP Entity ID is Okta's "Identity Provider Issuer" and Entra's "Microsoft Entra
Identifier". That mismatch is the whole reason an administrator ends up
searching the web mid-setup. All three values are in the federation metadata
document that every IdP publishes, in one standard shape — so one pasted URL
replaces the copying, and the guesswork with it.

**Only certificates are ever updated automatically.** A rollover is routine and
silent; a changed issuer or sign-in URL is not. Auto-applying those would mean a
single fetch could repoint where we send users to authenticate, and which issuer
we trust to vouch for them — a takeover primitive, not a convenience. Drift in
those fields is recorded for an administrator and left unapplied.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any

from lxml import etree

from user_service.identity import saml

logger = logging.getLogger(__name__)

NS = {
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}

BINDING_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
BINDING_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"

#: Metadata is bigger than an assertion (Entra's runs ~14 KB, a federation with
#: many certificates more), but it is still a document, not a download.
MAX_METADATA_BYTES = 1024 * 1024

FETCH_TIMEOUT_SECONDS = 15

#: How often the cron re-reads each provider's metadata, and how far ahead a
#: certificate has to expire before the provider card starts warning.
REFRESH_INTERVAL_SECONDS = 6 * 60 * 60
STARTUP_DELAY_SECONDS = 90
CERT_WARN_DAYS = 30


class MetadataError(Exception):
    """A refusal an administrator is allowed to read.

    Unlike `SamlError`, this one is safe to return in full: the caller holds
    `identity.sso.read` and is configuring the thing, so "no IDPSSODescriptor in
    that document" saves them an afternoon and tells an attacker nothing they
    could not learn by fetching the same public URL themselves.
    """


# ── parsing ───────────────────────────────────────────────────────────────

def parse_metadata(xml: bytes) -> dict[str, Any]:
    """Pull the SP-relevant half out of an IdP's federation metadata.

    Parsed with the same hardened parser the assertion path uses — entity
    resolution and network access off. Metadata arrives from a URL an
    administrator typed, which is exactly the shape of input that turns a
    friendly XML parser into an SSRF and file-disclosure primitive.
    """
    if not xml or not xml.strip():
        raise MetadataError("The document is empty.")
    if len(xml) > MAX_METADATA_BYTES:
        raise MetadataError(f"Metadata is larger than {MAX_METADATA_BYTES} bytes.")

    try:
        root = etree.fromstring(xml, parser=saml.hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise MetadataError(f"That is not valid XML: {exc}") from exc

    # An EntitiesDescriptor (a federation) can hold many entities; take the
    # first that actually describes an IdP rather than guessing.
    if etree.QName(root).localname == "EntitiesDescriptor":
        entity = next(
            (e for e in root.findall("md:EntityDescriptor", NS)
             if e.find("md:IDPSSODescriptor", NS) is not None), None)
        if entity is None:
            raise MetadataError("That federation document contains no identity provider.")
    else:
        entity = root

    if etree.QName(entity).localname != "EntityDescriptor":
        raise MetadataError(
            f"Expected an EntityDescriptor, found <{etree.QName(entity).localname}>.")

    idp = entity.find("md:IDPSSODescriptor", NS)
    if idp is None:
        raise MetadataError(
            "That document has no IDPSSODescriptor — it describes a service "
            "provider, not an identity provider. Check you copied the IdP's "
            "metadata URL and not your own.")

    entity_id = (entity.get("entityID") or "").strip()
    if not entity_id:
        raise MetadataError("The metadata declares no entityID.")

    sso_url = _binding_location(idp, "md:SingleSignOnService")
    if not sso_url:
        raise MetadataError("The metadata declares no SingleSignOnService location.")

    certs = _signing_certs(idp)
    if not certs:
        raise MetadataError("The metadata carries no signing certificate.")

    fmt = idp.find("md:NameIDFormat", NS)

    return {
        "idp_entity_id": entity_id,
        "idp_sso_url": sso_url,
        "idp_slo_url": _binding_location(idp, "md:SingleLogoutService"),
        "idp_x509_certs": certs,
        "name_id_format": (fmt.text or "").strip() if fmt is not None else None,
    }


def _binding_location(idp, tag: str) -> str | None:
    """Prefer HTTP-Redirect, which is the binding `build_authn_request` emits."""
    nodes = idp.findall(tag, NS)
    for wanted in (BINDING_REDIRECT, BINDING_POST):
        for n in nodes:
            if n.get("Binding") == wanted and (n.get("Location") or "").strip():
                return n.get("Location").strip()
    for n in nodes:                      # any binding beats nothing
        if (n.get("Location") or "").strip():
            return n.get("Location").strip()
    return None


def _signing_certs(idp) -> list[str]:
    """Certificates usable for signing, de-duplicated, document order kept.

    `use="encryption"` keys are skipped: publishing one does not mean the IdP
    signs with it, and feeding it to the verifier would only add a failing
    attempt to every login. An absent `use` means "any", per the schema.
    """
    out: list[str] = []
    for kd in idp.findall("md:KeyDescriptor", NS):
        if (kd.get("use") or "signing") != "signing":
            continue
        for node in kd.findall(".//ds:X509Certificate", NS):
            body = "".join((node.text or "").split())
            if body and body not in out:
                out.append(body)
    return out


# ── certificates ──────────────────────────────────────────────────────────

def _cert_body(cert: str) -> str:
    """The base64 payload, whether or not the value carries PEM armour.

    Both shapes are legitimate and both are in the wild: `_pem()` accepts either,
    so an administrator who pasted a whole `-----BEGIN CERTIFICATE-----` block
    has a working provider whose certificate this must still be able to read.
    Reading only the bare form meant exactly that provider — the one configured
    by hand rather than imported — silently never got an expiry warning.
    """
    text = (cert or "").strip()
    if "BEGIN CERTIFICATE" in text:
        text = text.split("-----BEGIN CERTIFICATE-----", 1)[-1]
        text = text.split("-----END CERTIFICATE-----", 1)[0]
    return "".join(text.split())


def cert_not_after(cert_b64: str) -> datetime | None:
    """Expiry of a certificate, PEM-armoured or bare base64; None if unreadable.

    Never raises: this feeds a warning badge, and a certificate we cannot parse
    must not take down the screen that would have told somebody about it.
    """
    try:
        from cryptography import x509

        der = base64.b64decode(_cert_body(cert_b64), validate=True)
        cert = x509.load_der_x509_certificate(der)
        value = getattr(cert, "not_valid_after_utc", None)
        if value is None:                       # cryptography < 42
            value = cert.not_valid_after.replace(tzinfo=timezone.utc)
        return value
    except Exception:
        return None


def cert_health(certs: list[str], *, now: datetime | None = None) -> list[dict]:
    """Per-certificate expiry, for the provider card to warn from."""
    now = now or datetime.now(timezone.utc)
    out = []
    for body in certs:
        not_after = cert_not_after(body)
        days = int((not_after - now).total_seconds() // 86400) if not_after else None
        out.append({
            # From the payload, not the raw string: a PEM-armoured value would
            # otherwise label every certificate "-----BEGIN CERTI".
            "fingerprint": _cert_body(body)[:16],
            "not_after": not_after.isoformat() if not_after else None,
            "days_remaining": days,
            "expired": bool(not_after and not_after <= now),
        })
    return out


def merge_certs(existing: list[str], incoming: list[str]) -> list[str]:
    """Add-only. A certificate is never removed by a refresh.

    Removal is the dangerous direction: a truncated response, a partially
    published rollover or the wrong URL could otherwise strip the one
    certificate that currently verifies, and every login would fail with the
    uniform 404 that hides the reason. Adding early is exactly what makes a
    rollover silent — both keys verify until the old one is taken out by hand.
    """
    merged = list(existing)
    for cert in incoming:
        if cert not in merged:
            merged.append(cert)
    return merged


# ── fetching ──────────────────────────────────────────────────────────────

async def fetch_metadata(url: str) -> bytes:
    """GET the metadata document, through the outbound guard.

    Re-validated here rather than trusting the write-time check: this runs from
    a scheduler against a URL living in a JSON column, which a migration or a
    DBA can change without passing through the API.
    """
    import httpx

    from velaris_shared.hxbridge.security import validate_outbound_url

    target = (url or "").strip()
    if not target.lower().startswith("https://"):
        raise MetadataError("A metadata URL must be https://.")
    try:
        await validate_outbound_url(target)
    except ValueError as exc:
        raise MetadataError(f"That URL is not allowed: {exc}") from exc

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = await client.get(target)
    except Exception as exc:
        raise MetadataError(f"Could not reach that URL: {exc}") from exc

    if resp.status_code != 200:
        raise MetadataError(f"That URL answered {resp.status_code}.")
    content = resp.content or b""
    if len(content) > MAX_METADATA_BYTES:
        raise MetadataError(f"Metadata is larger than {MAX_METADATA_BYTES} bytes.")
    return content


async def load_metadata(*, url: str | None = None, xml: str | None = None) -> dict[str, Any]:
    """Parse from a pasted document, or fetch and parse from a URL."""
    if xml and xml.strip():
        return parse_metadata(xml.encode() if isinstance(xml, str) else xml)
    if url and url.strip():
        return parse_metadata(await fetch_metadata(url))
    raise MetadataError("Give a metadata URL or paste the XML.")


# ── scheduled refresh ─────────────────────────────────────────────────────

def plan_refresh(config: dict, parsed: dict) -> tuple[dict, dict]:
    """What a refresh would change. Returns ``(new_config, report)``.

    Pure, so the decision is testable without a database or a network. The
    split it encodes is the whole safety story: certificates merge, everything
    else is only *reported*.
    """
    existing = [str(c) for c in (config.get("idp_x509_certs") or [])]
    merged = merge_certs(existing, parsed.get("idp_x509_certs") or [])
    added = [c for c in merged if c not in existing]

    drift = {}
    for key in ("idp_entity_id", "idp_sso_url"):
        current = str(config.get(key) or "").strip()
        found = str(parsed.get(key) or "").strip()
        if found and current and found != current:
            drift[key] = {"configured": current, "published": found}

    new_config = dict(config)
    new_config["idp_x509_certs"] = merged
    new_config["metadata_sync"] = {
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
        "certs_added": len(added),
        "drift": drift or None,
    }
    return new_config, {"certs_added": len(added), "drift": drift}


def record_failure(config: dict, message: str) -> dict:
    """A failed refresh changes nothing except the note saying it failed.

    Certificates, URLs and the enabled flag are all left exactly as they were —
    a network blip, a proxy hiccup or an IdP maintenance window must never be
    able to break a working sign-in.
    """
    new_config = dict(config)
    previous = dict(new_config.get("metadata_sync") or {})
    previous.update({
        "last_error": message[:300],
        "last_error_at": datetime.now(timezone.utc).isoformat(),
    })
    new_config["metadata_sync"] = previous
    return new_config


async def refresh_all(session) -> int:
    """One sweep over every SAML provider that has a metadata URL."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from velaris_shared.db.models import SsoProviderModel

    rows = (await session.execute(
        select(SsoProviderModel).where(SsoProviderModel.provider == "saml")
    )).scalars().all()

    touched = 0
    for row in rows:
        config = dict(row.config or {})
        url = str(config.get("idp_metadata_url") or "").strip()
        if not url:
            continue
        try:
            parsed = await load_metadata(url=url)
            new_config, report = plan_refresh(config, parsed)
            if report["certs_added"]:
                logger.info("saml metadata: provider %s gained %d certificate(s)",
                            row.id, report["certs_added"])
            if report["drift"]:
                logger.warning("saml metadata: provider %s has drifted from its "
                               "published metadata and was NOT changed: %s",
                               row.id, sorted(report["drift"]))
        except MetadataError as exc:
            new_config = record_failure(config, str(exc))
            logger.warning("saml metadata refresh failed for provider %s "
                           "(configuration left untouched): %s", row.id, exc)
        except Exception as exc:                        # never kill the sweep
            new_config = record_failure(config, str(exc))
            logger.warning("saml metadata refresh error for provider %s: %s", row.id, exc)

        row.config = new_config
        flag_modified(row, "config")
        touched += 1

    if touched:
        await session.commit()
    return touched


async def saml_metadata_cron() -> None:
    """Periodic refresh. Survives a bad cycle, like the other schedulers."""
    import asyncio

    from velaris_shared.db.session import get_session_factory

    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            async with get_session_factory()() as session:
                await refresh_all(session)
        except Exception as exc:
            logger.warning("saml metadata sweep failed (keeping previous): %s", exc)
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
