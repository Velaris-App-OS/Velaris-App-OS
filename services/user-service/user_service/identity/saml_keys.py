"""The service provider's own SAML key pair.

Velaris began as a *passive* SAML SP: it verified the IdP's signature and needed
no key of its own. Three things need one, and they all block on this module —
signed AuthnRequests, encrypted assertions, and SP-initiated logout.

**Where the private half lives.** Sealed with the tenant's HxVault DEK and stored
in `sso_providers.sp_key_enc`, mirroring `client_secret_enc`: a column rather
than a key inside `config`, because `config` is returned wholesale to the admin
console and key material must never be in a response body. The certificate is a
separate, *unencrypted* column — it is published in SP metadata for the IdP to
import, so treating it as a secret would only make it awkward to hand out.

**One pair per provider**, and one enabled SAML provider per tenant, so this is
effectively per tenant — matching the DEK it is sealed with. The seal is bound to
the provider id through its AAD, so a blob lifted into another provider's row
will not open.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

#: Long enough that rotation is a deliberate act rather than routine
#: maintenance. Rotation means replacing the pair *and* re-importing metadata at
#: the IdP, so an automatic rotation would be an outage with a timer on it.
CERT_VALIDITY_DAYS = 3 * 365
KEY_SIZE = 2048


class SpKeyError(Exception):
    """Key material is missing, unreadable, or cannot be created."""


def _aad(provider_id) -> bytes:
    """Binds the sealed key to one provider row.

    Without it, a sealed blob copied into another provider of the same tenant
    would open happily — and that provider might be pointed at a different IdP.
    """
    return b"velaris-saml-sp-key|" + str(provider_id).encode()


def generate_keypair(sp_entity_id: str) -> tuple[str, str]:
    """A fresh RSA key and a self-signed certificate. Returns ``(key, cert)`` PEM.

    Self-signed on purpose: this certificate is not a trust anchor for anybody.
    The IdP imports it out of band, from our metadata, and pins exactly this
    key — so a chain to a public CA would add cost and no assurance.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, (sp_entity_id or "velaris")[:64]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Velaris"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))     # tolerate clock skew
        .not_valid_after(now + timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


def cert_body(cert_pem: str) -> str:
    """The base64 payload, for embedding in metadata and XML."""
    text = (cert_pem or "").strip()
    if "BEGIN CERTIFICATE" in text:
        text = text.split("-----BEGIN CERTIFICATE-----", 1)[-1]
        text = text.split("-----END CERTIFICATE-----", 1)[0]
    return "".join(text.split())


async def _tenant_uuid(session, tenant_token) -> uuid.UUID | None:
    """Resolve a provider's tenant token to the UUID the DEK is keyed on.

    The provider row carries a slug (`dev`, `default`); `keyring._norm` wants a
    UUID and silently falls back to the *platform* DEK for anything else. Sealing
    one tenant's key under the platform DEK would still be safe, but it would
    make `shred()` for that tenant miss it — so resolve first, exactly as
    `identity_group_mappings` does.
    """
    if not tenant_token:
        return None
    from velaris_shared.tenancy.identity import resolve_tenant

    resolved = await resolve_tenant(session, str(tenant_token))
    return resolved[0] if resolved else None


async def ensure_keypair(session, row, *, regenerate: bool = False) -> str:
    """Return the provider's certificate PEM, creating the pair if needed.

    Idempotent: an existing pair is returned untouched unless `regenerate` is
    set, because silently minting a new key would break every assertion the IdP
    is already encrypting to the old one.
    """
    from velaris_shared.hxvault import crypto, keyring

    if row.sp_cert and row.sp_key_enc and not regenerate:
        return row.sp_cert

    cfg = dict(row.config or {})
    key_pem, cert_pem = generate_keypair(str(cfg.get("sp_entity_id") or "velaris"))

    tid = await _tenant_uuid(session, row.tenant_id)
    try:
        dek = await keyring.ensure_dek(session, tid)
    except Exception as exc:
        raise SpKeyError(f"HxVault could not provide a key for this tenant: {exc}") from exc

    sealed = crypto.seal(dek, key_pem.encode(), _aad(row.id))
    row.sp_key_enc = {"v": 1, "blob": base64.b64encode(sealed).decode()}
    row.sp_cert = cert_pem
    row.sp_key_created_at = datetime.now(timezone.utc)
    logger.info("saml: %s SP key pair for provider %s",
                "regenerated" if regenerate else "created", row.id)
    return cert_pem


async def load_private_key(session, row) -> str:
    """The provider's private key PEM. Raises rather than returning a blank.

    A caller that treated "no key" as "do not sign" would silently downgrade a
    provider configured to sign — which is the failure an IdP rejects with an
    error the administrator cannot read.
    """
    from velaris_shared.hxvault import crypto, keyring

    enc = dict(row.sp_key_enc or {})
    blob = enc.get("blob")
    if not blob:
        raise SpKeyError("This provider has no SP key pair. Generate one first.")

    tid = await _tenant_uuid(session, row.tenant_id)
    dek = keyring.cached_dek(tid)
    if dek is None:
        dek = await keyring.ensure_dek(session, tid)
    try:
        return crypto.open_(dek, base64.b64decode(blob), _aad(row.id)).decode()
    except Exception as exc:
        raise SpKeyError(
            "The SP key could not be decrypted. If the deployment's key material "
            "changed, regenerate the pair and re-import the metadata at the IdP."
        ) from exc


def key_summary(row) -> dict | None:
    """What the admin screen may see: everything except the key itself."""
    if not row.sp_cert:
        return None
    from user_service.identity.saml_metadata import cert_health

    health = cert_health([row.sp_cert])
    return {
        "created_at": row.sp_key_created_at.isoformat() if row.sp_key_created_at else None,
        "not_after": health[0]["not_after"],
        "days_remaining": health[0]["days_remaining"],
        "expired": health[0]["expired"],
        "fingerprint": health[0]["fingerprint"],
    }
