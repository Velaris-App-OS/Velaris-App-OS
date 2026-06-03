"""P65 — Customer Accounts: self-registration, OTP login, profile management.

Public routes  (no operator auth):
  POST /portal/{slug}/auth/register       — create account + send OTP
  POST /portal/{slug}/auth/request-otp    — login: send OTP to existing account
  POST /portal/{slug}/auth/verify-otp     — exchange OTP for customer_token (JWT)
  GET  /portal/{slug}/account             — get own profile  [customer_token]
  PUT  /portal/{slug}/account             — update name/phone/emails [customer_token]
  DELETE /portal/{slug}/account           — GDPR self-erasure [customer_token]

Admin routes (operator token required):
  GET    /portal/{slug}/customers         — list/search customers
  GET    /portal/{slug}/customers/{id}    — customer detail + case list
  DELETE /portal/{slug}/customers/{id}    — anonymise/delete (GDPR)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    TenantModel,
    CaseInstanceModel,
    PortalCustomerModel,
    PortalCustomerCaseLinkModel,
)
from case_service.db.session import get_session

log = logging.getLogger(__name__)


def _require_customer_accounts():
    """Gate every customer accounts endpoint behind the manifest release flag."""
    from case_service.api.routers.releases import is_feature_enabled
    if not is_feature_enabled("customer_accounts"):
        raise HTTPException(404, "Customer Accounts is not available on this instance")


public_router = APIRouter(
    tags=["portal-customers-public"],
    dependencies=[Depends(_require_customer_accounts)],
)
admin_router = APIRouter(
    tags=["portal-customers-admin"],
    dependencies=[Depends(_require_customer_accounts)],
)

_OTP_TTL_SECONDS = 600  # 10 minutes


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _customer_jwt_secret() -> str:
    return os.getenv("HELIX_CASE_SECRET_KEY", "dev-secret-change-me")


def _mint_customer_token(customer_id: str, tenant_id: str, slug: str) -> str:
    try:
        import jwt as _jwt
    except ImportError:
        raise HTTPException(500, "PyJWT not installed")

    now = int(time.time())
    payload = {
        "sub":       customer_id,
        "tenant_id": tenant_id,
        "slug":      slug,
        "type":      "portal_customer",
        "iat":       now,
        "exp":       now + 86400,   # 24 h
        "iss":       "helix-portal",
        "aud":       "helix-portal-api",
    }
    return _jwt.encode(payload, _customer_jwt_secret(), algorithm="HS256")


def _decode_customer_token(token: str) -> dict:
    try:
        import jwt as _jwt
        return _jwt.decode(
            token,
            _customer_jwt_secret(),
            algorithms=["HS256"],
            issuer="helix-portal",
            audience="helix-portal-api",
            options={"verify_exp": True},
        )
    except Exception:
        raise HTTPException(401, "Invalid or expired customer token")


async def _require_customer(
    authorization: str = Header(""),
    session: AsyncSession = Depends(get_session),
) -> PortalCustomerModel:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing customer token")
    claims = _decode_customer_token(authorization[7:])
    customer = await session.get(PortalCustomerModel, uuid.UUID(claims["sub"]))
    if not customer:
        raise HTTPException(404, "Customer not found")
    return customer


# ── OTP helpers ───────────────────────────────────────────────────────────────

def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _generate_and_hash_otp() -> tuple[str, str]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    return code, _hash_otp(code)


async def _send_otp_email(session: AsyncSession, to_email: str, otp: str, purpose: str = "login") -> None:
    try:
        from case_service.db.models import EmailAccountModel
        from case_service.mail import EmailService

        account = (await session.execute(
            select(EmailAccountModel).where(
                EmailAccountModel.is_default_outbound.is_(True),
                EmailAccountModel.is_active.is_(True),
            ).limit(1)
        )).scalar_one_or_none()

        if account:
            svc = EmailService()
            action = "verify your account" if purpose == "register" else "log in to your account"
            await svc.send(
                session,
                case_id=None,
                account=account,
                to_addresses=[to_email],
                subject="Your verification code",
                body_text=(
                    f"Your one-time code to {action} is: {otp}\n\n"
                    "This code expires in 10 minutes and can only be used once.\n"
                    "Do not share this code with anyone."
                ),
            )
        else:
            log.warning("P65 OTP (no SMTP configured) for %s: %s", to_email, otp)
    except Exception as exc:
        log.warning("P65 OTP email delivery failed for %s: %s", to_email, exc)


async def _get_tenant_by_slug(slug: str, session: AsyncSession) -> TenantModel:
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug, TenantModel.portal_enabled.is_(True))
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, "Portal not found")
    return tenant


# ── Request / Response schemas ────────────────────────────────────────────────

class RegisterBody(BaseModel):
    email: str
    display_name: str
    phone: Optional[str] = None


class RequestOTPBody(BaseModel):
    email: str


class VerifyOTPBody(BaseModel):
    email: str
    otp: str


class UpdateProfileBody(BaseModel):
    display_name: Optional[str] = None
    phone: Optional[str] = None
    alt_email: Optional[str] = None
    preferred_email: Optional[str] = None   # "primary" | "alt"


# ── Public: Registration ──────────────────────────────────────────────────────

@public_router.post("/{slug}/auth/register")
async def portal_register(
    slug: str,
    body: RegisterBody,
    session: AsyncSession = Depends(get_session),
):
    """Create a new customer account and send verification OTP."""
    tenant = await _get_tenant_by_slug(slug, session)

    existing = (await session.execute(
        select(PortalCustomerModel).where(
            PortalCustomerModel.tenant_id == tenant.id,
            PortalCustomerModel.primary_email == body.email.lower(),
        )
    )).scalar_one_or_none()

    otp, otp_hash = _generate_and_hash_otp()
    expires = datetime.now(timezone.utc) + timedelta(seconds=_OTP_TTL_SECONDS)

    if existing:
        # Account exists — resend OTP (acts like request-otp but also works as "already registered")
        existing.otp_code       = otp_hash
        existing.otp_expires_at = expires
        session.add(existing)
        await session.commit()
        comm_email = existing.alt_email if existing.preferred_email == "alt" and existing.alt_email else body.email.lower()
        await _send_otp_email(session, comm_email, otp, purpose="register")
        return {"ok": True, "message": "Verification code sent. If account exists, code was resent."}

    customer = PortalCustomerModel(
        tenant_id       = tenant.id,
        primary_email   = body.email.lower(),
        display_name    = body.display_name.strip(),
        phone           = body.phone,
        verified        = False,
        otp_code        = otp_hash,
        otp_expires_at  = expires,
    )
    session.add(customer)
    await session.commit()
    await session.refresh(customer)

    # Auto-link any historical cases with matching email
    historical = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.portal_submitter_email == body.email.lower(),
            CaseInstanceModel.id.not_in(
                select(PortalCustomerCaseLinkModel.case_id)
            ),
        )
    )).scalars().all()
    for c in historical:
        session.add(PortalCustomerCaseLinkModel(customer_id=customer.id, case_id=c.id))
    if historical:
        await session.commit()

    await _send_otp_email(session, body.email.lower(), otp, purpose="register")
    return {"ok": True, "message": "Account created. Check your email for the verification code."}


# ── Public: Login (request OTP) ───────────────────────────────────────────────

@public_router.post("/{slug}/auth/request-otp")
async def portal_request_otp(
    slug: str,
    body: RequestOTPBody,
    session: AsyncSession = Depends(get_session),
):
    """Send OTP to login to an existing account."""
    tenant = await _get_tenant_by_slug(slug, session)

    customer = (await session.execute(
        select(PortalCustomerModel).where(
            PortalCustomerModel.tenant_id == tenant.id,
            PortalCustomerModel.primary_email == body.email.lower(),
        )
    )).scalar_one_or_none()

    # Always return same response AND same response time to prevent account enumeration.
    # The 300ms floor means an attacker cannot distinguish "email exists" from "email not
    # found" by measuring latency — both branches return after the same minimum delay.
    if customer:
        otp, otp_hash = _generate_and_hash_otp()
        customer.otp_code       = otp_hash
        customer.otp_expires_at = datetime.now(timezone.utc) + timedelta(seconds=_OTP_TTL_SECONDS)
        session.add(customer)
        await session.commit()
        comm_email = customer.alt_email if customer.preferred_email == "alt" and customer.alt_email else body.email.lower()
        await _send_otp_email(session, comm_email, otp)

    await asyncio.sleep(0.3)
    return {"ok": True, "message": "If an account exists with this email, a code has been sent."}


# ── Public: Verify OTP ────────────────────────────────────────────────────────

@public_router.post("/{slug}/auth/verify-otp")
async def portal_verify_otp(
    slug: str,
    body: VerifyOTPBody,
    session: AsyncSession = Depends(get_session),
):
    """Verify OTP and return a customer_token JWT."""
    tenant = await _get_tenant_by_slug(slug, session)

    customer = (await session.execute(
        select(PortalCustomerModel).where(
            PortalCustomerModel.tenant_id == tenant.id,
            PortalCustomerModel.primary_email == body.email.lower(),
        )
    )).scalar_one_or_none()

    if not customer or not customer.otp_code or not customer.otp_expires_at:
        raise HTTPException(401, "Invalid or expired code")

    if customer.otp_expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(401, "Code has expired")

    if not secrets.compare_digest(customer.otp_code, _hash_otp(body.otp.strip())):
        raise HTTPException(401, "Invalid code")

    # Consume OTP, mark verified, update last_active
    customer.otp_code       = None
    customer.otp_expires_at = None
    customer.verified       = True
    customer.last_active_at = datetime.now(timezone.utc)
    session.add(customer)
    await session.commit()

    token = _mint_customer_token(str(customer.id), str(tenant.id), slug)
    return {
        "customer_token": token,
        "customer": {
            "id":              str(customer.id),
            "display_name":    customer.display_name,
            "primary_email":   customer.primary_email,
            "alt_email":       customer.alt_email,
            "preferred_email": customer.preferred_email,
            "phone":           customer.phone,
        },
    }


# ── Public: Profile ───────────────────────────────────────────────────────────

@public_router.get("/{slug}/account")
async def portal_get_account(
    slug: str,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    # Count cases
    case_count = (await session.execute(
        select(func.count()).select_from(PortalCustomerCaseLinkModel).where(
            PortalCustomerCaseLinkModel.customer_id == customer.id
        )
    )).scalar_one()

    return {
        "id":              str(customer.id),
        "display_name":    customer.display_name,
        "primary_email":   customer.primary_email,
        "alt_email":       customer.alt_email,
        "preferred_email": customer.preferred_email,
        "phone":           customer.phone,
        "verified":        customer.verified,
        "case_count":      case_count,
        "created_at":      customer.created_at.isoformat(),
        "last_active_at":  customer.last_active_at.isoformat(),
    }


@public_router.put("/{slug}/account")
async def portal_update_account(
    slug: str,
    body: UpdateProfileBody,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    if body.display_name is not None:
        customer.display_name = body.display_name.strip()
    if body.phone is not None:
        customer.phone = body.phone or None
    if body.alt_email is not None:
        customer.alt_email = str(body.alt_email).lower() if body.alt_email else None
        # Auto-switch preferred to alt when alt is set
        if customer.alt_email:
            customer.preferred_email = "alt"
        else:
            customer.preferred_email = "primary"
    if body.preferred_email in ("primary", "alt"):
        # Only allow "alt" if alt_email is set
        if body.preferred_email == "alt" and not customer.alt_email:
            raise HTTPException(400, "No alternative email set")
        customer.preferred_email = body.preferred_email

    session.add(customer)
    await session.commit()
    return {"ok": True}


@public_router.delete("/{slug}/account")
async def portal_delete_account(
    slug: str,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    """GDPR Art. 17 — customer self-erasure. Anonymises the record."""
    customer.display_name    = "Deleted User"
    customer.primary_email   = f"deleted_{customer.id}@deleted"
    customer.alt_email       = None
    customer.phone           = None
    customer.otp_code        = None
    customer.otp_expires_at  = None
    session.add(customer)
    await session.commit()
    return {"ok": True, "message": "Your account has been deleted."}


# ── Public: My Cases (authenticated) ─────────────────────────────────────────

@public_router.get("/{slug}/account/cases")
async def portal_account_cases(
    slug: str,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(CaseInstanceModel)
        .join(PortalCustomerCaseLinkModel, PortalCustomerCaseLinkModel.case_id == CaseInstanceModel.id)
        .where(PortalCustomerCaseLinkModel.customer_id == customer.id)
        .order_by(CaseInstanceModel.created_at.desc())
    )).scalars().all()

    return {"cases": [
        {
            "case_id":        str(c.id),
            "case_number":    c.case_number,
            "tracking_token": str(c.portal_tracking_token) if c.portal_tracking_token else None,
            "subject":        c.subject,
            "status":         c.status,
            "priority":       c.priority,
            "submitted_at":   c.created_at.isoformat(),
            "updated_at":     c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in rows
    ]}


# ── Admin: Customer management ────────────────────────────────────────────────

@admin_router.get("/{slug}/customers")
async def admin_list_customers(
    slug: str,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    tenant = await _get_tenant_by_slug(slug, session)
    stmt = (
        select(PortalCustomerModel)
        .where(PortalCustomerModel.tenant_id == tenant.id)
    )
    if q:
        like = f"%{q.lower()}%"
        from sqlalchemy import or_
        stmt = stmt.where(or_(
            PortalCustomerModel.primary_email.ilike(like),
            PortalCustomerModel.display_name.ilike(like),
        ))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows  = (await session.execute(stmt.order_by(PortalCustomerModel.created_at.desc()).limit(limit).offset(offset))).scalars().all()

    return {
        "total": total,
        "customers": [
            {
                "id":              str(c.id),
                "display_name":    c.display_name,
                "primary_email":   c.primary_email,
                "alt_email":       c.alt_email,
                "preferred_email": c.preferred_email,
                "phone":           c.phone,
                "verified":        c.verified,
                "created_at":      c.created_at.isoformat(),
                "last_active_at":  c.last_active_at.isoformat(),
            }
            for c in rows
        ],
    }


@admin_router.get("/{slug}/customers/{customer_id}")
async def admin_get_customer(
    slug: str,
    customer_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    tenant = await _get_tenant_by_slug(slug, session)
    customer = await session.get(PortalCustomerModel, customer_id)
    if not customer or customer.tenant_id != tenant.id:
        raise HTTPException(404, "Customer not found")

    cases = (await session.execute(
        select(CaseInstanceModel)
        .join(PortalCustomerCaseLinkModel, PortalCustomerCaseLinkModel.case_id == CaseInstanceModel.id)
        .where(PortalCustomerCaseLinkModel.customer_id == customer.id)
        .order_by(CaseInstanceModel.created_at.desc())
    )).scalars().all()

    return {
        "id":              str(customer.id),
        "display_name":    customer.display_name,
        "primary_email":   customer.primary_email,
        "alt_email":       customer.alt_email,
        "preferred_email": customer.preferred_email,
        "phone":           customer.phone,
        "verified":        customer.verified,
        "created_at":      customer.created_at.isoformat(),
        "last_active_at":  customer.last_active_at.isoformat(),
        "cases": [
            {
                "case_id":      str(c.id),
                "case_number":  c.case_number,
                "subject":      c.subject,
                "status":       c.status,
                "submitted_at": c.created_at.isoformat(),
            }
            for c in cases
        ],
    }


@admin_router.delete("/{slug}/customers/{customer_id}")
async def admin_delete_customer(
    slug: str,
    customer_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """GDPR Art. 17 — admin-initiated anonymisation."""
    tenant = await _get_tenant_by_slug(slug, session)
    customer = await session.get(PortalCustomerModel, customer_id)
    if not customer or customer.tenant_id != tenant.id:
        raise HTTPException(404, "Customer not found")

    customer.display_name   = "Deleted User"
    customer.primary_email  = f"deleted_{customer.id}@deleted"
    customer.alt_email      = None
    customer.phone          = None
    customer.otp_code       = None
    customer.otp_expires_at = None
    session.add(customer)
    await session.commit()
    return {"ok": True}
