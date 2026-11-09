"""Internal API for user-service (R5e step 10) — what sign-in may not do itself.

Mounted under `/internal/v1/user/…` by `internal_api/router.py`, behind the same
guard as the portal's and commerce's routes (internal service secret, mTLS when
internal TLS is on, refused when it arrives through a proxy).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("case_service.internal_api.user")


# ── 1. a password-reset code by email ─────────────────────────────────────────

class UserOtpEmailIn(BaseModel):
    purpose: str = Field(pattern="^password_reset$")
    to: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")
    #: Minutes the code is valid for — user-service's OTP_VALID_MINS.
    valid_minutes: int = Field(ge=1, le=60)


async def otp_email(session: AsyncSession, body: UserOtpEmailIn) -> dict:
    """Composed here, never by the caller: there is no route that sends what a
    caller wrote — only an address, a 6-digit code and its lifetime. The address
    must be an account's, so this cannot be used to mail anyone else.

    Until this route existed the reset code was never sent at all: auth_real
    imported a `MailService` that `case_service.mail.service` does not define,
    and a bare `except: pass` swallowed the ImportError (found in the R5e step 10
    analysis, 2026-10-21). Delivery failures are logged, never raised.
    """
    from velaris_sdk.config.brand import BRAND_NAME

    from case_service.mail import EmailService
    from velaris_shared.db.models import EmailAccountModel, HelixUserModel

    try:
        user = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.email == body.to)
        )).scalar_one_or_none()
        if user is None:
            log.warning("password-reset email refused: no account has that address")
            return {"sent": False}
        account = (await session.execute(
            select(EmailAccountModel).where(
                EmailAccountModel.is_default_outbound.is_(True),
                EmailAccountModel.is_active.is_(True),
            ).limit(1)
        )).scalar_one_or_none()
        if account is None:
            # Unlike the portal's development fallback, the code is NOT logged:
            # it resets a staff password.
            log.warning("password-reset email not sent: no default outbound email account")
            return {"sent": False}
        name = user.display_name or user.username
        text = (f"Hi {name},\n\n"
                f"Your password reset code is: {body.code}\n\n"
                f"This code expires in {body.valid_minutes} minutes and can only be used once.\n"
                "If you did not request this, ignore this email.\n\n"
                f"— {BRAND_NAME}")
        await EmailService().send(
            session, case_id=None, account=account, to_addresses=[user.email],
            subject=f"{BRAND_NAME} — password reset code", body_text=text,
            secret_body=True)
        return {"sent": True}
    except Exception as exc:  # noqa: BLE001
        log.warning("password-reset email delivery failed: %s", type(exc).__name__)
        return {"sent": False}


#: path (under /internal/v1/) -> (request model, handler)
ROUTES: dict[str, tuple[type[BaseModel], Any]] = {
    "user/otp-email": (UserOtpEmailIn, otp_email),
}
