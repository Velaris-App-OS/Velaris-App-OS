"""XML Encryption — just enough of it to open an `EncryptedAssertion`.

`signxml` verifies signatures but does no decryption, and `xmlsec` was ruled out
for the same reason `pysaml2` was: it needs the `xmlsec1` *binary*, which would
put an apt package in the critical path of a product installed by a shell
script. XML-Enc's hybrid scheme is small and well specified, so the subset a
SAML SP actually meets is implemented here over `cryptography`.

The shape is always the same: an `EncryptedKey` holds a symmetric key sealed to
our RSA public key, and an `EncryptedData` holds the assertion sealed under that
symmetric key.

**Two algorithm decisions carry the security of this module.**

*RSA-1.5 is refused.* PKCS#1 v1.5 key transport is what Bleichenbacher's attack
and the Jager–Somorovsky XML-Enc breaks target, and an SP that accepts it can be
walked into decrypting chosen ciphertext. Every IdP worth federating with offers
OAEP; one that offers only v1.5 is a misconfiguration to fix, not a compatibility
case to serve.

*CBC is supported, and every failure is identical.* AES-CBC is the default of
both Entra and Okta, so refusing it would make this feature useless — but CBC
without authentication is a padding oracle whenever the attacker can tell padding
failure from any other. Every path here raises the same `DecryptionError` with no
distinguishing detail, and the ACS turns every one of those into the same
uniform 400. GCM is preferred whenever the IdP offers it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import logging

from lxml import etree

logger = logging.getLogger(__name__)

NS_XENC = "http://www.w3.org/2001/04/xmlenc#"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS = {"xenc": NS_XENC, "ds": NS_DS,
      "saml": "urn:oasis:names:tc:SAML:2.0:assertion"}

#: Key transport. OAEP only — see the module docstring.
RSA_OAEP = "http://www.w3.org/2001/04/xmlenc#rsa-oaep-mgf1p"
RSA_OAEP_11 = "http://www.w3.org/2009/xmlenc11#rsa-oaep"
RSA_1_5 = "http://www.w3.org/2001/04/xmlenc#rsa-1_5"

#: Content encryption, and the IV length each one prefixes to its ciphertext.
BLOCK_CIPHERS = {
    f"{NS_XENC}aes128-cbc": ("cbc", 16),
    f"{NS_XENC}aes192-cbc": ("cbc", 16),
    f"{NS_XENC}aes256-cbc": ("cbc", 16),
    "http://www.w3.org/2009/xmlenc11#aes128-gcm": ("gcm", 12),
    "http://www.w3.org/2009/xmlenc11#aes192-gcm": ("gcm", 12),
    "http://www.w3.org/2009/xmlenc11#aes256-gcm": ("gcm", 12),
}

GCM_TAG_BYTES = 16


class DecryptionError(Exception):
    """Uniform. Never carries which step failed — that is the padding oracle."""


def _b64(node) -> bytes:
    if node is None or not (node.text or "").strip():
        raise DecryptionError("encrypted_assertion")
    try:
        return base64.b64decode("".join((node.text or "").split()), validate=True)
    except Exception as exc:
        raise DecryptionError("encrypted_assertion") from exc


def _algorithm(parent) -> str:
    method = parent.find("./xenc:EncryptionMethod", NS)
    if method is None:
        raise DecryptionError("encrypted_assertion")
    return method.get("Algorithm") or ""


def _unwrap_key(encrypted_key, private_key_pem: str) -> bytes:
    """Open the `EncryptedKey` with our private key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    algorithm = _algorithm(encrypted_key)
    if algorithm == RSA_1_5:
        # Named explicitly in the log because this one is worth an admin's
        # attention: it is a configuration choice at the IdP, not an attack.
        logger.warning("saml: IdP used RSA-1.5 key transport, which is refused; "
                       "switch the app to RSA-OAEP")
        raise DecryptionError("encrypted_assertion")
    if algorithm not in (RSA_OAEP, RSA_OAEP_11):
        raise DecryptionError("encrypted_assertion")

    # `DigestMethod` selects the OAEP hash; SHA-1 remains the XML-Enc default and
    # is safe *inside* OAEP, where it is not relied on for collision resistance.
    digest_el = encrypted_key.find("./xenc:EncryptionMethod/ds:DigestMethod", NS)
    digest_url = (digest_el.get("Algorithm") if digest_el is not None else "") or ""
    digest = hashes.SHA256() if digest_url.endswith("sha256") else hashes.SHA1()

    cipher_value = encrypted_key.find("./xenc:CipherData/xenc:CipherValue", NS)
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    try:
        return key.decrypt(
            _b64(cipher_value),
            padding.OAEP(mgf=padding.MGF1(algorithm=digest), algorithm=digest, label=None),
        )
    except Exception as exc:
        raise DecryptionError("encrypted_assertion") from exc


def _decrypt_data(encrypted_data, session_key: bytes) -> bytes:
    """Open the `EncryptedData` with the unwrapped symmetric key."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    algorithm = _algorithm(encrypted_data)
    if algorithm not in BLOCK_CIPHERS:
        raise DecryptionError("encrypted_assertion")
    mode, iv_len = BLOCK_CIPHERS[algorithm]

    blob = _b64(encrypted_data.find("./xenc:CipherData/xenc:CipherValue", NS))
    if len(blob) <= iv_len:
        raise DecryptionError("encrypted_assertion")
    iv, body = blob[:iv_len], blob[iv_len:]

    try:
        if mode == "gcm":
            if len(body) <= GCM_TAG_BYTES:
                raise DecryptionError("encrypted_assertion")
            return AESGCM(session_key).decrypt(iv, body, None)

        decryptor = Cipher(algorithms.AES(session_key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(body) + decryptor.finalize()
        if not padded:
            raise DecryptionError("encrypted_assertion")
        # XML-Enc padding: the final byte is the padding length. Validated, but
        # the refusal is the same object raised everywhere else in this module,
        # so a caller cannot distinguish a padding failure from a wrong key.
        pad = padded[-1]
        if pad < 1 or pad > len(padded):
            raise DecryptionError("encrypted_assertion")
        return padded[:-pad]
    except DecryptionError:
        raise
    except Exception as exc:
        raise DecryptionError("encrypted_assertion") from exc


def decrypt_assertion(encrypted_assertion, private_key_pem: str, *, parser=None):
    """Return the `Assertion` element inside an `EncryptedAssertion`.

    The result is re-parsed with the hardened parser rather than trusted as a
    string: plaintext recovered from an attacker-supplied ciphertext is still
    attacker-influenced XML, and it has not been through the entity and size
    defences the outer document went through.
    """
    from user_service.identity.saml import hardened_parser

    encrypted_data = encrypted_assertion.find("./xenc:EncryptedData", NS)
    if encrypted_data is None:
        raise DecryptionError("encrypted_assertion")

    # The EncryptedKey may sit inside the EncryptedData's KeyInfo or beside it.
    encrypted_key = encrypted_data.find("./ds:KeyInfo/xenc:EncryptedKey", NS)
    if encrypted_key is None:
        encrypted_key = encrypted_assertion.find("./xenc:EncryptedKey", NS)
    if encrypted_key is None:
        encrypted_key = encrypted_data.find(".//xenc:EncryptedKey", NS)
    if encrypted_key is None:
        raise DecryptionError("encrypted_assertion")

    session_key = _unwrap_key(encrypted_key, private_key_pem)
    plaintext = _decrypt_data(encrypted_data, session_key)

    try:
        element = etree.fromstring(plaintext, parser=parser or hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise DecryptionError("encrypted_assertion") from exc

    if etree.QName(element).localname != "Assertion":
        raise DecryptionError("encrypted_assertion")
    return element


def has_encrypted_assertion(document) -> bool:
    return document.find("./saml:EncryptedAssertion", NS) is not None
