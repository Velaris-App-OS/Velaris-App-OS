"""Group J — WebAuthn (FIDO2 passkeys) ceremonies.

Thin orchestration over py_webauthn: challenge lifecycle, credential
storage, and sign-count enforcement live here; all cryptographic
verification (CBOR, COSE, attestation, assertion signatures) is delegated
to the library.

Challenges are single-use DB rows with a 5-minute expiry. The verify step
looks the row up by the exact challenge bytes echoed in the client's
clientDataJSON — race-free across processes and concurrent logins — and
deletes it before verification, so a replayed response finds nothing.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from case_service.config import get_settings
from case_service.db.models import WebAuthnChallengeModel, WebAuthnCredentialModel

log = logging.getLogger(__name__)

CHALLENGE_TTL_MINUTES = 5


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def challenge_from_credential(credential: dict) -> bytes:
    """Extract the challenge echoed in the client's clientDataJSON.

    Used only to LOCATE our challenge row; the cryptographic check that the
    signed response really covers this challenge is py_webauthn's job.
    """
    client_data = json.loads(_b64url_decode(credential["response"]["clientDataJSON"]))
    return _b64url_decode(client_data["challenge"])


async def _store_challenge(
    session: AsyncSession, challenge: bytes, purpose: str, user_id: str | None,
) -> None:
    # Sweep expired rows while we're here — keeps the table tiny
    await session.execute(
        delete(WebAuthnChallengeModel).where(
            WebAuthnChallengeModel.expires_at < datetime.now(timezone.utc)
        )
    )
    session.add(WebAuthnChallengeModel(
        id=uuid.uuid4(),
        user_id=user_id,
        challenge=challenge,
        purpose=purpose,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_TTL_MINUTES),
    ))
    await session.flush()


async def _consume_challenge(
    session: AsyncSession, challenge: bytes, purpose: str,
) -> WebAuthnChallengeModel | None:
    """Fetch-and-delete: a challenge verifies at most once."""
    row = (await session.execute(
        select(WebAuthnChallengeModel)
        .where(WebAuthnChallengeModel.challenge == challenge)
        .where(WebAuthnChallengeModel.purpose == purpose)
        .where(WebAuthnChallengeModel.expires_at > datetime.now(timezone.utc))
    )).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.flush()
    return row


async def _user_credentials(
    session: AsyncSession, user_id: str,
) -> list[WebAuthnCredentialModel]:
    return list((await session.execute(
        select(WebAuthnCredentialModel)
        .where(WebAuthnCredentialModel.user_id == user_id)
        .where(WebAuthnCredentialModel.revoked_at.is_(None))
    )).scalars().all())


# ── Registration (enrollment) ────────────────────────────────────────


async def begin_registration(
    session: AsyncSession, user_id: str, username: str, display_name: str,
) -> str:
    """Returns PublicKeyCredentialCreationOptions as a JSON string."""
    s = get_settings()
    existing = await _user_credentials(session, user_id)
    options = generate_registration_options(
        rp_id=s.webauthn_rp_id,
        rp_name=s.webauthn_rp_name,
        user_id=user_id.encode("utf-8"),
        user_name=username,
        user_display_name=display_name or username,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,      # discoverable when possible
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    await _store_challenge(session, options.challenge, "register", user_id)
    return options_to_json(options)


async def complete_registration(
    session: AsyncSession, user_id: str, credential: dict, device_name: str,
) -> WebAuthnCredentialModel:
    """Verify the attestation response and persist the new passkey."""
    s = get_settings()
    challenge = challenge_from_credential(credential)
    row = await _consume_challenge(session, challenge, "register")
    if row is None or row.user_id != user_id:
        raise ValueError("Registration challenge not found or expired — try again.")

    verified = verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=s.webauthn_rp_id,
        expected_origin=s.webauthn_origin,
        require_user_verification=False,  # UV preferred, not required (platform variance)
    )

    cred = WebAuthnCredentialModel(
        id=uuid.uuid4(),
        user_id=user_id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=credential.get("response", {}).get("transports", []) or [],
        aaguid=str(verified.aaguid) if verified.aaguid else None,
        device_name=(device_name or "Passkey")[:255],
    )
    session.add(cred)
    await session.flush()
    log.info("webauthn: passkey registered for user %s", user_id)
    return cred


# ── Authentication (login / step-up) ─────────────────────────────────


async def begin_authentication(
    session: AsyncSession, user_id: str | None, purpose: str = "login",
) -> str:
    """Returns PublicKeyCredentialRequestOptions as a JSON string.

    With user_id: allow-list that user's passkeys (and bind the challenge to
    them — used by step-up). Without: discoverable/usernameless login.
    """
    s = get_settings()
    allow = None
    if user_id:
        creds = await _user_credentials(session, user_id)
        if not creds:
            raise ValueError("No passkeys registered for this account.")
        allow = [PublicKeyCredentialDescriptor(id=c.credential_id) for c in creds]

    options = generate_authentication_options(
        rp_id=s.webauthn_rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    await _store_challenge(session, options.challenge, purpose, user_id)
    return options_to_json(options)


async def complete_authentication(
    session: AsyncSession, credential: dict, purpose: str = "login",
    expected_user_id: str | None = None,
) -> tuple[WebAuthnCredentialModel, bool]:
    """Verify an assertion. Returns (credential row, user_verified).

    expected_user_id (step-up) additionally pins both the challenge row and
    the credential to that user, so one user's assertion can never satisfy
    another user's step-up prompt.

    user_verified reports whether the authenticator performed user
    verification (biometric/PIN) — UP alone proves possession, UP+UV proves
    possession AND identity. Callers decide what their context requires:
    step-up demands UV (it replaces password+TOTP); login demands UV for
    TOTP-enrolled accounts so a passkey never lowers their two-factor bar.
    """
    s = get_settings()
    challenge = challenge_from_credential(credential)
    row = await _consume_challenge(session, challenge, purpose)
    if row is None:
        raise ValueError("Authentication challenge not found or expired — try again.")
    if expected_user_id is not None and row.user_id != expected_user_id:
        raise ValueError("Challenge does not belong to this user.")

    raw_id = _b64url_decode(credential["rawId"])
    cred = (await session.execute(
        select(WebAuthnCredentialModel)
        .where(WebAuthnCredentialModel.credential_id == raw_id)
        .where(WebAuthnCredentialModel.revoked_at.is_(None))
    )).scalar_one_or_none()
    if cred is None:
        raise ValueError("Unknown passkey.")
    if expected_user_id is not None and cred.user_id != expected_user_id:
        raise ValueError("Passkey does not belong to this user.")

    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=s.webauthn_rp_id,
        expected_origin=s.webauthn_origin,
        credential_public_key=cred.public_key,
        credential_current_sign_count=cred.sign_count,
        require_user_verification=False,
    )

    # Sign-count regression = possible cloned authenticator. py_webauthn
    # raises on regression when counters are in use; counters of 0 mean the
    # authenticator doesn't implement them (passkeys synced via iCloud/Google
    # commonly report 0) and are accepted.
    cred.sign_count = verified.new_sign_count
    cred.last_used_at = datetime.now(timezone.utc)
    await session.flush()
    return cred, bool(verified.user_verified)
