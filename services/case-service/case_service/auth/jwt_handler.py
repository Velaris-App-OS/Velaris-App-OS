"""JWT token creation and validation.

Supports:
- HS256 mode: symmetric shared secret (dev / legacy).  Prints a startup warning.
- RS256 mode: asymmetric RSA key pair (recommended for production).
              Set HELIX_CASE_AUTH_RSA_PRIVATE_KEY + HELIX_CASE_AUTH_RSA_PUBLIC_KEY.
- OIDC mode:  validates RS256 tokens from Keycloak / Auth0 / etc.

RS256 is the default when RSA keys are configured.  The signing private key lives
only in the auth service; every other service holds only the public key and
therefore cannot mint tokens — unlike the shared-secret HS256 model.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_jwt = None


def _get_jwt():
    global _jwt
    if _jwt is None:
        try:
            import jwt as _jwt_lib
            _jwt = _jwt_lib
        except ImportError:
            logger.warning("PyJWT not installed — JWT validation unavailable")
            _jwt = None
    return _jwt


# ── Key generation helper ─────────────────────────────────────────────────────

def generate_rsa_keypair(key_size: int = 2048) -> tuple[str, str]:
    """Generate an RSA key pair and return (private_pem, public_pem).

    Uses the `cryptography` library when available; falls back to the system
    `openssl` binary (present on every Linux/macOS server).

    Also prints the values so they can be pasted into .env / a secrets manager.

    Usage:
        python -c "from case_service.auth.jwt_handler import generate_rsa_keypair; generate_rsa_keypair()"
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    except ImportError:
        # Fall back to openssl CLI — available on every Linux/macOS server.
        import subprocess, tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            priv_path = os.path.join(tmp, "priv.pem")
            pub_path  = os.path.join(tmp, "pub.pem")
            subprocess.run(
                ["openssl", "genrsa", "-out", priv_path, str(key_size)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["openssl", "rsa", "-in", priv_path, "-pubout", "-out", pub_path],
                check=True, capture_output=True,
            )
            with open(priv_path) as f:
                private_pem = f.read()
            with open(pub_path) as f:
                public_pem = f.read()

    print("# ── RSA key pair generated ──────────────────────────────────────────")
    print("# Add both to your .env file (replace literal newlines with \\n):")
    print(f"HELIX_CASE_AUTH_RSA_PRIVATE_KEY={repr(private_pem)}")
    print(f"HELIX_CASE_AUTH_RSA_PUBLIC_KEY={repr(public_pem)}")
    return private_pem, public_pem


# ── Token creation ────────────────────────────────────────────────────────────

def create_dev_token(
    user_id: str,
    username: str = "Developer",
    roles: list[str] | None = None,
    secret: str = "",
    expire_days: int = 60,
    expire_minutes: int = 0,
    private_key: str = "",
    jti: str = "",
) -> str:
    """Create a signed JWT token.

    Args:
        private_key:     PEM-encoded RSA private key. When provided, RS256 is used.
                         When absent, HS256 is used (dev only).
        roles:           Explicit role list. Defaults to [] (fail closed, never admin).
        expire_minutes:  When non-zero, overrides expire_days for short-lived tokens.
        jti:             JWT ID for revocation tracking. Auto-generated if not provided.
    """
    jwt = _get_jwt()
    if jwt is None:
        raise RuntimeError("PyJWT not installed")

    import os
    from uuid import uuid4
    email_domain = os.getenv("HELIX_AUTH_EMAIL_DOMAIN", "local")
    now = int(time.time())

    # SECURITY FIX: roles default is [] (fail closed), never ["admin"].
    effective_roles: list[str] = roles if roles is not None else []

    exp = now + (expire_minutes * 60 if expire_minutes else expire_days * 86400)

    payload = {
        "sub": user_id,
        "preferred_username": username,
        "email": f"{username}@{email_domain}",
        "realm_access": {"roles": effective_roles},
        "iat": now,
        "exp": exp,
        "iss": "helix",
        "aud": "helix-api",
        "jti": jti or str(uuid4()),
    }

    if private_key:
        return jwt.encode(payload, private_key, algorithm="RS256")

    # HS256 fallback — only acceptable in local dev.
    logger.warning(
        "Signing JWT with HS256 shared secret. "
        "Set HELIX_CASE_AUTH_RSA_PRIVATE_KEY + HELIX_CASE_AUTH_RSA_PUBLIC_KEY "
        "to enable RS256 before deploying to production."
    )
    return jwt.encode(payload, secret, algorithm="HS256")


# ── Token verification ────────────────────────────────────────────────────────

def decode_jwt_token(
    token: str,
    secret: str,
    issuer: str = "helix",
    audience: str = "helix-api",
    public_key: str = "",
) -> dict[str, Any]:
    """Decode and validate a JWT token.

    Algorithm is determined internally — never caller-supplied.
    RS256 when public_key is provided; HS256 otherwise.
    This prevents algorithm-confusion attacks (alg:none, HS256/RS256 swap).

    Args:
        public_key: PEM-encoded RSA public key. When provided the token is
                    verified with RS256 and secret is ignored.
        secret:     HS256 shared secret. Used only when public_key is empty.
    """
    jwt = _get_jwt()
    if jwt is None:
        raise RuntimeError("PyJWT not installed")

    if public_key:
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"verify_exp": True},
        )

    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        issuer=issuer,
        audience=audience,
        options={"verify_exp": True},
    )


# ── Claims extraction ─────────────────────────────────────────────────────────

def extract_user_from_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """Extract user info from JWT claims (Keycloak format)."""
    roles = []

    # Keycloak realm roles
    realm_access = claims.get("realm_access", {})
    if isinstance(realm_access, dict):
        roles.extend(realm_access.get("roles", []))

    # Keycloak resource roles
    resource_access = claims.get("resource_access", {})
    if isinstance(resource_access, dict):
        for resource in resource_access.values():
            if isinstance(resource, dict):
                roles.extend(resource.get("roles", []))

    # Simple roles claim
    if "roles" in claims and isinstance(claims["roles"], list):
        roles.extend(claims["roles"])

    roles = list(set(roles))

    return {
        "user_id": claims.get("sub", "unknown"),
        "username": claims.get("preferred_username", claims.get("name", "")),
        "email": claims.get("email", ""),
        "roles": roles,
        "groups": claims.get("groups", []),
        "department": claims.get("department", ""),
    }
