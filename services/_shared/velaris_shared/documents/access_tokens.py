"""Short-lived, document-scoped download grants for anonymous portal customers.

**Why this exists.** Until 2026-08-15 every document in the deployment was
readable by UUID with no authentication at all — `GET /documents/{id}/download`
had no auth dependency and no check in its body. The reason it was open is that
`portal.py` hands anonymous customers `download_url` values pointing straight at
it, so simply gating the route would have broken customer document access. But
the same route serves every *internal* document too, so the portal's need
explained the hole without justifying its blast radius.

The fix keeps both properties. The portal has **already made the authorization
decision** when it lists a case's documents — it checks the tracking token or
the submitter email, and only returns documents that are `portal_visible` or the
customer's own upload. This module carries that decision forward as a signed
grant instead of leaving the door open for everyone:

    mint(document_id)  ->  "v1.<expiry>.<signature>"

The grant is **bound to one document id**, so a token minted for the customer's
own upload cannot fetch an internal document, and **expires**, so a link shared
or logged does not become a permanent capability.

Design notes worth keeping:

* **The key is purpose-derived, not `auth_secret` itself.** Signing with the raw
  JWT secret would mean a leak of either one compromised both, and it invites
  cross-protocol confusion (a document grant is not a token anyone should be
  able to feed to the JWT decoder). `HMAC(auth_secret, "velaris.document-access.v1")`
  gives a subkey that cannot be reversed to the secret.
* **Rotating `auth_secret` invalidates outstanding links.** That is acceptable
  precisely because they are short-lived — and it is the opposite of the HxVault
  KEK trap, where deriving from `auth_secret` silently destroyed data at rest.
  Nothing here is at rest; the worst case is a customer clicking a stale link.
* **Constant-time comparison**, because signature verification with `==` leaks
  the prefix a forger has guessed correctly.
* The token is deliberately NOT a JWT. It carries one claim, it is checked in
  one place, and giving it a JWT's shape would tempt someone to accept it where
  a real token belongs.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import uuid

log = logging.getLogger(__name__)

#: Version prefix, so the format can change without accepting old shapes blindly.
_VERSION = "v1"

#: Domain separation for the derived key. Changing this string invalidates every
#: outstanding grant, which is the intended way to revoke them all at once.
_PURPOSE = b"velaris.document-access.v1"

#: Long enough for a customer to click the link in a page they are already
#: looking at; short enough that a copied URL is not a lasting capability.
DEFAULT_TTL_SECONDS = 15 * 60


def _key() -> bytes:
    from velaris_shared.config import get_settings

    secret = (get_settings().auth_secret or "").encode()
    return hmac.new(secret, _PURPOSE, hashlib.sha256).digest()


def _sign(document_id: str, expires_at: int) -> str:
    payload = f"{_VERSION}.{document_id}.{expires_at}".encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def mint(document_id: uuid.UUID | str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """A grant to download exactly this document, valid for `ttl_seconds`."""
    expires_at = int(time.time()) + int(ttl_seconds)
    return f"{_VERSION}.{expires_at}.{_sign(str(document_id), expires_at)}"


def verify(token: str | None, document_id: uuid.UUID | str) -> bool:
    """Is `token` a live grant for THIS document? Never raises; False on anything
    unexpected, because every failure mode here has to be a refusal."""
    if not token:
        return False
    try:
        version, expiry_raw, signature = token.split(".", 2)
        if version != _VERSION:
            return False
        expires_at = int(expiry_raw)
        if expires_at < int(time.time()):
            return False
        expected = _sign(str(document_id), expires_at)
        # Binding to the document id lives INSIDE the signature: a grant for
        # another document produces a different `expected` and fails here, so
        # there is no separate ownership check to forget.
        return hmac.compare_digest(expected, signature)
    except Exception:                      # malformed, wrong types, bad base64
        return False


def download_url(document_id: uuid.UUID | str, *,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """The URL the portal hands an anonymous customer."""
    return f"/api/v1/documents/{document_id}/download?grant={mint(document_id, ttl_seconds=ttl_seconds)}"
