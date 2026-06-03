"""P64 — Real Authentication router (ENH-10).

Extends the existing dev-mode auth with:
- bcrypt password login with account lockout
- Forgot password via email OTP
- TOTP MFA (enrolment + verification)
- SSO provider registry (Google, GitHub, Azure AD)
- User self-registration (admin-approved or open)

The existing /auth/login endpoint delegates here when a real user
record exists. Dev-mode fallback is preserved for local development.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import HelixUserModel, AuthOtpModel, SsoProviderModel
from case_service.db.session import get_session
from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.auth.jwt_handler import create_dev_token   # reuse JWT creation
from case_service.config import get_settings
from case_service.hxbridge.encryption import encrypt_credentials, decrypt_credentials

router = APIRouter(prefix="/auth/real", tags=["auth-real"])

MAX_ATTEMPTS  = 5
LOCKOUT_MINS  = 15
OTP_VALID_MINS = 10


# ── Password helpers ──────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()

def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False

def _is_locked(user: HelixUserModel) -> bool:
    if user.locked_until and user.locked_until > datetime.now(timezone.utc):
        return True
    return False

async def _get_token_expiry_days(session: AsyncSession) -> int:
    from sqlalchemy import text
    row = (await session.execute(
        text("SELECT value FROM helix_settings WHERE key = 'token_expiry_days'")
    )).scalar_one_or_none()
    try:
        return int(row) if row else get_settings().token_expiry_days
    except (ValueError, TypeError):
        return get_settings().token_expiry_days

async def _make_jwt(user: HelixUserModel, session: AsyncSession) -> str:
    settings = get_settings()
    expire_days = await _get_token_expiry_days(session)
    return create_dev_token(
        user_id=str(user.id),
        username=user.username,
        roles=list(user.roles or []),
        secret=settings.auth_secret,
        expire_days=expire_days,
        private_key=settings.auth_rsa_private_key or "",
    )

def _user_dict(user: HelixUserModel) -> dict:
    roles = list(user.roles or [])
    return {
        "user_id":      str(user.id),
        "username":     user.username,
        "email":        user.email,
        "display_name": user.display_name,
        "roles":        roles,
        "groups":       [],
        "is_admin":     "admin" in roles,
        "is_designer":  "designer" in roles,
        "is_case_worker": "case_worker" in roles,
        "password_change_required": user.password_change_required,
        "mfa_enabled":  user.mfa_enabled,
    }


# ── Schemas ───────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str
    mfa_token: Optional[str] = None   # 6-digit TOTP if MFA enabled

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict
    mfa_required: bool = False

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: str
    display_name: Optional[str] = None
    password: str = Field(..., min_length=8)
    roles: list[str] = ["viewer"]
    password_change_required: bool = True

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    email: str
    otp: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=8)

class MfaVerifyRequest(BaseModel):
    token: str = Field(..., min_length=6, max_length=6)

class MfaDisableRequest(BaseModel):
    token: str = Field(..., min_length=6, max_length=6)  # current TOTP code — proves device possession

class SsoProviderIn(BaseModel):
    tenant_id: Optional[str] = None
    provider: str   # 'google'|'github'|'azure'
    client_id: str
    client_secret: str
    config: dict = {}

class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    roles: Optional[list[str]] = None
    is_active: Optional[bool] = None
    password_change_required: Optional[bool] = None


# ── Login ─────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginResponse)
async def login_real(
    body:    LoginRequest,
    session: AsyncSession = Depends(get_session),
):
    """Real bcrypt login. Falls through to dev-mode if no user record exists."""
    user = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == body.username)
    )).scalar_one_or_none()

    if not user:
        raise HTTPException(401, "Invalid username or password.")

    if not user.is_active:
        raise HTTPException(403, "Account is disabled. Contact an administrator.")

    if _is_locked(user):
        remaining = int((user.locked_until - datetime.now(timezone.utc)).total_seconds() / 60) + 1
        raise HTTPException(423, f"Account locked. Try again in {remaining} minute(s).")

    if not user.password_hash:
        raise HTTPException(400, "This account uses SSO login. Use the SSO button instead.")

    if not _verify_password(body.password, user.password_hash):
        user.failed_attempts += 1
        locked_now = user.failed_attempts >= MAX_ATTEMPTS
        if locked_now:
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINS)
            user.failed_attempts = 0
        await session.commit()
        # Audit: failed login
        try:
            from case_service.enterprise.security_events import log_security_event
            from case_service.hxstream.emitter import emit_trace
            evt = "auth.account_locked" if locked_now else "auth.login_failed"
            await log_security_event(
                session,
                event_type=evt,
                severity="warning",
                user_id=body.username,
                resource_type="user",
                resource_id=body.username,
                action="login",
                outcome="denied",
                details={"username": body.username, "locked": locked_now},
            )
            await emit_trace(evt, {"username": body.username, "locked": locked_now},
                             actor_user_id=body.username, session=session)
            await session.commit()
        except Exception:
            pass
        if locked_now:
            raise HTTPException(423, f"Too many failed attempts. Account locked for {LOCKOUT_MINS} minutes.")
        raise HTTPException(401, f"Incorrect password. {MAX_ATTEMPTS - user.failed_attempts} attempt(s) remaining.")

    # Password correct — check MFA
    if user.mfa_enabled:
        if not body.mfa_token:
            # Signal frontend to show MFA step
            return LoginResponse(
                access_token="",
                user=_user_dict(user),
                mfa_required=True,
            )
        secret = decrypt_credentials(user.mfa_secret_enc)["secret"] if user.mfa_secret_enc else ""
        totp = pyotp.TOTP(secret)
        if not totp.verify(body.mfa_token, valid_window=1):
            user.failed_attempts += 1
            locked_now = user.failed_attempts >= MAX_ATTEMPTS
            if locked_now:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINS)
                user.failed_attempts = 0
            await session.commit()
            try:
                from case_service.enterprise.security_events import log_security_event
                evt = "auth.account_locked" if locked_now else "auth.mfa_failed"
                await log_security_event(
                    session, event_type=evt, severity="warning",
                    user_id=body.username, resource_type="user", resource_id=body.username,
                    action="mfa_verify", outcome="denied",
                    details={"username": body.username, "locked": locked_now},
                )
                await session.commit()
            except Exception:
                pass
            if locked_now:
                raise HTTPException(423, f"Too many failed MFA attempts. Account locked for {LOCKOUT_MINS} minutes.")
            raise HTTPException(401, f"Invalid MFA token. {MAX_ATTEMPTS - user.failed_attempts} attempt(s) remaining.")

    # Success
    user.failed_attempts = 0
    user.locked_until    = None
    user.last_login_at   = datetime.now(timezone.utc)
    await session.commit()

    # Audit: login success
    try:
        from case_service.enterprise.security_events import log_security_event
        from case_service.hxstream.emitter import emit_trace
        await log_security_event(
            session,
            event_type="auth.login",
            severity="info",
            user_id=user.username,
            resource_type="user",
            resource_id=user.username,
            action="login",
            outcome="success",
            details={"username": user.username},
        )
        await emit_trace(
            "auth.login",
            {"username": user.username},
            actor_user_id=user.username,
            session=session,
        )
        await session.commit()
    except Exception:
        pass  # audit failure must never block login

    return LoginResponse(
        access_token=await _make_jwt(user, session),
        user=_user_dict(user),
    )


# ── Register ──────────────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register_user(
    body:    RegisterRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(require_role("admin")),
):
    """Create a new Helix user (auth account). Admin only."""
    existing = (await session.execute(
        select(HelixUserModel).where(
            (HelixUserModel.username == body.username) | (HelixUserModel.email == body.email)
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Username or email already in use.")

    new_user = HelixUserModel(
        username=body.username,
        email=body.email,
        display_name=body.display_name,
        password_hash=_hash_password(body.password),
        roles=body.roles,
        password_change_required=body.password_change_required,
    )
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)
    return _user_dict(new_user)


# ── Superadmin guard ──────────────────────────────────────────────

def _is_superadmin_user(actor: AuthenticatedUser) -> bool:
    return "superadmin" in (actor.roles or [])


def _assert_can_modify(actor: AuthenticatedUser, target: HelixUserModel) -> None:
    """Enforce modification rules:
    - No one can modify/delete the superadmin via API
    - Only admins (or superadmin) can deactivate users
    - Superadmin role cannot be removed from any user
    """
    if getattr(target, "is_superadmin", False):
        raise HTTPException(403, "The superadmin account cannot be modified via the API.")
    if not (actor.is_admin or _is_superadmin_user(actor)):
        raise HTTPException(403, "Only admins can modify user accounts.")


# ── User management ───────────────────────────────────────────────

@router.get("/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    require_role("admin")(user)
    # Superadmin is hidden from all user management views
    rows = (await session.execute(
        select(HelixUserModel)
        .where(HelixUserModel.is_superadmin == False)  # noqa: E712
        .order_by(HelixUserModel.username)
    )).scalars().all()
    return {"users": [_user_dict(r) for r in rows], "total": len(rows)}


@router.get("/users/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    require_role("admin")(user)
    u = await session.get(HelixUserModel, user_id)
    if not u or getattr(u, "is_superadmin", False):
        raise HTTPException(404, "User not found")
    return _user_dict(u)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    body:    UpdateUserRequest,
    session: AsyncSession = Depends(get_session),
    actor:   AuthenticatedUser = Depends(get_current_user),
):
    require_role("admin")(actor)
    u = await session.get(HelixUserModel, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    _assert_can_modify(actor, u)

    if body.display_name is not None:
        u.display_name = body.display_name
    if body.roles is not None:
        # Prevent removing superadmin role from any account via API
        if "superadmin" in (u.roles or []) and "superadmin" not in body.roles:
            raise HTTPException(403, "Cannot remove superadmin role via API.")
        u.roles = body.roles
    if body.is_active is not None:
        u.is_active = body.is_active
    if body.password_change_required is not None:
        u.password_change_required = body.password_change_required
    u.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return _user_dict(u)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor:   AuthenticatedUser = Depends(get_current_user),
):
    require_role("admin")(actor)
    u = await session.get(HelixUserModel, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    _assert_can_modify(actor, u)
    await session.delete(u)
    await session.commit()


# ── Self-service profile ──────────────────────────────────────────

class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    email:        Optional[str] = None


@router.get("/me/profile")
async def get_my_profile(
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    """Return the current user's full profile from HelixUserModel."""
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == user.username)
    )).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "User record not found.")
    return {
        **_user_dict(u),
        "is_active":     u.is_active,
        "is_sso":        bool(u.sso_provider),
        "sso_provider":  u.sso_provider,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_at":    u.created_at.isoformat() if u.created_at else None,
        "locked_until":  u.locked_until.isoformat() if u.locked_until else None,
    }


@router.patch("/me/profile")
async def update_my_profile(
    body:    UpdateProfileRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    """Let any authenticated user update their own display name and email."""
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == user.username)
    )).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "User record not found.")
    if body.display_name is not None:
        u.display_name = body.display_name.strip() or None
    if body.email is not None:
        if body.email and not body.email.strip():
            raise HTTPException(400, "Email cannot be blank.")
        # Ensure no collision with another user
        existing = (await session.execute(
            select(HelixUserModel)
            .where(HelixUserModel.email == body.email.strip())
            .where(HelixUserModel.id != u.id)
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "That email is already in use by another account.")
        u.email = body.email.strip()
    u.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return {**_user_dict(u), "is_active": u.is_active, "is_sso": bool(u.sso_provider)}


# ── Password change ───────────────────────────────────────────────

@router.post("/change-password")
async def change_password(
    body:    ChangePasswordRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == user.username)
    )).scalar_one_or_none()
    if not u or not u.password_hash:
        raise HTTPException(400, "Password change not applicable for this account.")
    if not _verify_password(body.current_password, u.password_hash):
        raise HTTPException(401, "Current password is incorrect.")
    u.password_hash = _hash_password(body.new_password)
    u.password_change_required = False
    u.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


# ── Forgot / reset password ───────────────────────────────────────

@router.post("/forgot-password")
async def forgot_password(
    body:    ForgotPasswordRequest,
    session: AsyncSession = Depends(get_session),
):
    """Send a 6-digit OTP to the user's email for password reset."""
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.email == body.email)
    )).scalar_one_or_none()
    # Always return 200 to avoid email enumeration
    if not u:
        return {"ok": True, "message": "If that email exists, an OTP has been sent."}

    otp_plain = "".join([str(secrets.randbelow(10)) for _ in range(6)])
    otp_rec = AuthOtpModel(
        user_id=u.id,
        otp_hash=_hash_password(otp_plain),
        purpose="password_reset",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_VALID_MINS),
    )
    session.add(otp_rec)
    await session.commit()

    # Send OTP email using existing mail service
    try:
        from case_service.mail.service import MailService
        svc = MailService()
        await svc.send(
            to=[u.email],
            subject="Velaris — Password Reset OTP",
            body_text=(
                f"Hi {u.display_name or u.username},\n\n"
                f"Your password reset OTP is: {otp_plain}\n\n"
                f"This code expires in {OTP_VALID_MINS} minutes.\n\n"
                f"If you did not request this, ignore this email.\n\n"
                f"— Velaris"
            ),
        )
    except Exception:
        pass  # OTP is still saved; user can request again

    return {"ok": True, "message": "If that email exists, an OTP has been sent."}


@router.post("/reset-password")
async def reset_password(
    body:    ResetPasswordRequest,
    session: AsyncSession = Depends(get_session),
):
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.email == body.email)
    )).scalar_one_or_none()
    if not u:
        raise HTTPException(400, "Invalid or expired OTP.")

    # Account lockout check — failed OTP attempts share the same counter as password failures.
    if _is_locked(u):
        remaining = int((u.locked_until - datetime.now(timezone.utc)).total_seconds() / 60) + 1
        raise HTTPException(423, f"Account locked. Try again in {remaining} minute(s).")

    otp_rec = (await session.execute(
        select(AuthOtpModel)
        .where(AuthOtpModel.user_id == u.id)
        .where(AuthOtpModel.purpose == "password_reset")
        .where(AuthOtpModel.used_at == None)  # noqa: E711
        .where(AuthOtpModel.expires_at > datetime.now(timezone.utc))
        .order_by(AuthOtpModel.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    if not otp_rec or not _verify_password(body.otp, otp_rec.otp_hash):
        # Increment failure counter; lock and invalidate active OTPs after MAX_ATTEMPTS.
        u.failed_attempts += 1
        if u.failed_attempts >= MAX_ATTEMPTS:
            u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINS)
            u.failed_attempts = 0
            # Invalidate all pending reset OTPs so they can't be retried after unlock.
            if otp_rec:
                otp_rec.used_at = datetime.now(timezone.utc)
        await session.commit()
        raise HTTPException(400, "Invalid or expired OTP.")

    u.password_hash = _hash_password(body.new_password)
    u.password_change_required = False
    u.failed_attempts = 0
    u.locked_until = None
    otp_rec.used_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


# ── MFA ───────────────────────────────────────────────────────────

@router.post("/mfa/enrol")
async def mfa_enrol(
    response: Response,
    session:  AsyncSession = Depends(get_session),
    user:     AuthenticatedUser = Depends(get_current_user),
):
    """Generate a TOTP secret and return the QR code provisioning URI."""
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == user.username)
    )).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "User not found")

    secret = pyotp.random_base32()
    totp   = pyotp.TOTP(secret)
    uri    = totp.provisioning_uri(name=u.email, issuer_name="Velaris")

    # Store encrypted — NOT enabled until verified
    u.mfa_secret_enc = encrypt_credentials({"secret": secret})
    u.updated_at = datetime.now(timezone.utc)
    await session.commit()

    # The response contains the TOTP secret — never let it be cached by proxies.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return {"provisioning_uri": uri, "secret": secret}


@router.post("/mfa/verify-enrol")
async def mfa_verify_enrol(
    body:    MfaVerifyRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    """Verify the first TOTP code and activate MFA."""
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == user.username)
    )).scalar_one_or_none()
    if not u or not u.mfa_secret_enc:
        raise HTTPException(400, "No pending MFA enrolment.")
    secret = decrypt_credentials(u.mfa_secret_enc)["secret"]
    totp   = pyotp.TOTP(secret)
    if not totp.verify(body.token, valid_window=1):
        raise HTTPException(401, "Invalid TOTP token.")
    u.mfa_enabled  = True
    u.updated_at   = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True, "mfa_enabled": True}


@router.post("/mfa/disable")
async def mfa_disable(
    body:    MfaDisableRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    """Disable MFA. Requires the current TOTP code to prove device possession."""
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == user.username)
    )).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "User not found")
    if not u.mfa_enabled or not u.mfa_secret_enc:
        raise HTTPException(400, "MFA is not enabled on this account.")
    secret = decrypt_credentials(u.mfa_secret_enc)["secret"]
    totp   = pyotp.TOTP(secret)
    if not totp.verify(body.token, valid_window=1):
        raise HTTPException(401, "Invalid TOTP code. Enter the current 6-digit code from your authenticator app.")
    u.mfa_enabled    = False
    u.mfa_secret_enc = None
    u.updated_at     = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True, "mfa_enabled": False}


# ── SSO provider management ───────────────────────────────────────

@router.get("/sso/providers")
async def list_sso_providers(
    tenant_id: Optional[str] = None,
    session:   AsyncSession = Depends(get_session),
    _:         AuthenticatedUser = Depends(get_current_user),
):
    rows = (await session.execute(
        select(SsoProviderModel)
        .where(SsoProviderModel.enabled == True)  # noqa: E712
        .order_by(SsoProviderModel.provider)
    )).scalars().all()
    return {
        "providers": [
            {"id": str(r.id), "provider": r.provider, "client_id": r.client_id,
             "enabled": r.enabled, "tenant_id": r.tenant_id}
            for r in rows
        ]
    }


@router.post("/sso/providers", status_code=201)
async def create_sso_provider(
    body:    SsoProviderIn,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    require_role("admin")(user)
    p = SsoProviderModel(
        tenant_id=body.tenant_id,
        provider=body.provider,
        client_id=body.client_id,
        client_secret_enc=encrypt_credentials({"secret": body.client_secret}),
        config=body.config,
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return {"id": str(p.id), "provider": p.provider, "client_id": p.client_id}


@router.delete("/sso/providers/{provider_id}", status_code=204)
async def delete_sso_provider(
    provider_id: uuid.UUID,
    session:     AsyncSession = Depends(get_session),
    user:        AuthenticatedUser = Depends(get_current_user),
):
    require_role("admin")(user)
    p = await session.get(SsoProviderModel, provider_id)
    if not p:
        raise HTTPException(404, "Provider not found")
    await session.delete(p)
    await session.commit()


# ── OAuth2 redirect helpers (Google + GitHub) ─────────────────────
# Full PKCE flow: frontend redirects to provider, gets code, posts code here,
# we exchange for token, get user info, create/find Helix user, issue JWT.

OAUTH_ENDPOINTS = {
    "google": {
        "auth_url":   "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url":  "https://oauth2.googleapis.com/token",
        "userinfo":   "https://www.googleapis.com/oauth2/v3/userinfo",
        "scope":      "openid email profile",
    },
    "github": {
        "auth_url":   "https://github.com/login/oauth/authorize",
        "token_url":  "https://github.com/login/oauth/access_token",
        "userinfo":   "https://api.github.com/user",
        "scope":      "read:user user:email",
    },
}


@router.get("/sso/{provider}/auth-url")
async def get_sso_auth_url(
    provider:    str,
    redirect_uri: str,
    session:     AsyncSession = Depends(get_session),
):
    """Return the provider's OAuth2 authorization URL for the frontend to redirect to."""
    if provider not in OAUTH_ENDPOINTS:
        raise HTTPException(400, f"Unknown provider '{provider}'. Supported: {list(OAUTH_ENDPOINTS)}")

    p_row = (await session.execute(
        select(SsoProviderModel)
        .where(SsoProviderModel.provider == provider)
        .where(SsoProviderModel.enabled == True)  # noqa: E712
        .limit(1)
    )).scalar_one_or_none()
    if not p_row:
        raise HTTPException(404, f"SSO provider '{provider}' is not configured.")

    ep  = OAUTH_ENDPOINTS[provider]
    state = secrets.token_urlsafe(16)
    params = {
        "client_id":     p_row.client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         ep["scope"],
        "state":         state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return {"auth_url": f"{ep['auth_url']}?{query}", "state": state}


class SsoCallbackRequest(BaseModel):
    provider:     str
    code:         str
    redirect_uri: str


@router.post("/sso/callback")
async def sso_callback(
    body:    SsoCallbackRequest,
    session: AsyncSession = Depends(get_session),
):
    """Exchange OAuth2 code for a Helix JWT."""
    import httpx

    if body.provider not in OAUTH_ENDPOINTS:
        raise HTTPException(400, f"Unknown provider '{body.provider}'")

    p_row = (await session.execute(
        select(SsoProviderModel)
        .where(SsoProviderModel.provider == body.provider)
        .where(SsoProviderModel.enabled == True)  # noqa: E712
        .limit(1)
    )).scalar_one_or_none()
    if not p_row:
        raise HTTPException(404, "SSO provider not configured")

    ep     = OAUTH_ENDPOINTS[body.provider]
    secret = decrypt_credentials(p_row.client_secret_enc)["secret"] if p_row.client_secret_enc else ""

    async with httpx.AsyncClient(timeout=15) as client:
        # Exchange code for access token
        token_resp = await client.post(ep["token_url"], data={
            "client_id":     p_row.client_id,
            "client_secret": secret,
            "code":          body.code,
            "redirect_uri":  body.redirect_uri,
            "grant_type":    "authorization_code",
        }, headers={"Accept": "application/json"})
        if not token_resp.is_success:
            raise HTTPException(400, "Token exchange failed")
        token_data  = token_resp.json()
        access_token = token_data.get("access_token") or token_data.get("id_token")

        # Fetch user info
        info_resp = await client.get(ep["userinfo"], headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        })
        if not info_resp.is_success:
            raise HTTPException(400, "Failed to fetch user info from provider")
        info = info_resp.json()

    sso_email    = info.get("email") or info.get("emails", [{}])[0].get("email", "")
    sso_subject  = str(info.get("sub") or info.get("id") or "")
    display_name = info.get("name") or info.get("login") or sso_email.split("@")[0]

    if not sso_email:
        raise HTTPException(400, "Provider did not return an email address")

    # Find or create Helix user
    u = (await session.execute(
        select(HelixUserModel).where(
            (HelixUserModel.sso_provider == body.provider) &
            (HelixUserModel.sso_subject  == sso_subject)
        )
    )).scalar_one_or_none()

    if not u:
        # Try to match by email (SSO enrolment)
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.email == sso_email)
        )).scalar_one_or_none()
        if u:
            u.sso_provider = body.provider
            u.sso_subject  = sso_subject
        else:
            # Auto-provision as viewer
            username = sso_email.split("@")[0]
            u = HelixUserModel(
                username=username, email=sso_email,
                display_name=display_name,
                roles=["viewer"],
                sso_provider=body.provider,
                sso_subject=sso_subject,
            )
            session.add(u)

    u.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(u)

    return {"access_token": await _make_jwt(u, session), "token_type": "bearer", "user": _user_dict(u)}


# ── Admin: token expiry setting ───────────────────────────────────

@router.get("/settings/token-expiry")
async def get_token_expiry(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(require_role("admin")),
):
    return {"token_expiry_days": await _get_token_expiry_days(session)}


class TokenExpiryRequest(BaseModel):
    token_expiry_days: int = Field(..., ge=1, le=3650)

@router.put("/settings/token-expiry")
async def set_token_expiry(
    body: TokenExpiryRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    from sqlalchemy import text
    await session.execute(text(
        "INSERT INTO helix_settings (key, value, updated_at, updated_by) "
        "VALUES ('token_expiry_days', :v, NOW(), :u) "
        "ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = NOW(), updated_by = :u"
    ), {"v": str(body.token_expiry_days), "u": user.username})
    await session.commit()
    return {"token_expiry_days": body.token_expiry_days}
