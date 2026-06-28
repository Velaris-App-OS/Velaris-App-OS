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

import hashlib
import secrets
import time as _time
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import HelixUserModel, AuthOtpModel, SsoProviderModel, RefreshTokenModel, RevokedSessionModel, HelixSettingModel
from case_service.db.session import get_auth_session as get_session
from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.auth.jwt_handler import create_dev_token
from case_service.config import get_settings
from case_service.hxbridge.encryption import encrypt_credentials, decrypt_credentials

_bearer = HTTPBearer(auto_error=False)

router = APIRouter(prefix="/auth/real", tags=["auth-real"])

MAX_ATTEMPTS  = 5
LOCKOUT_MINS  = 15
OTP_VALID_MINS = 10

# ── D4: per-IP rate limits on credential endpoints ────────────────────────────
# Complements the per-user lockout (MAX_ATTEMPTS): the lockout stops attacks
# on one account; these stop one IP from spraying across many accounts.
from case_service.middleware.endpoint_rate_limit import rate_limit

_login_rl    = rate_limit(max_calls=10, window_seconds=60,  name="login")
_register_rl = rate_limit(max_calls=10, window_seconds=300, name="registration")
_forgot_rl   = rate_limit(max_calls=5,  window_seconds=300, name="password-reset")
_reset_rl    = rate_limit(max_calls=5,  window_seconds=300, name="password-reset")

# ── Refresh-endpoint rate limiter (sliding window, per-process) ───────────────
# Bounds: 10 requests per IP per 60 s. Memory: O(unique_IPs * 10 * 8 bytes).
# Multi-worker deployments multiply this by worker count — still safe given
# the 288-bit token entropy makes brute-force infeasible at any rate.
_REFRESH_RATES: dict[str, deque] = {}
_REFRESH_MAX    = 10
_REFRESH_WINDOW = 60.0   # seconds


def _check_refresh_rate(ip: str) -> bool:
    """Return False when the IP has exceeded _REFRESH_MAX calls in _REFRESH_WINDOW s."""
    now = _time.monotonic()
    if ip not in _REFRESH_RATES:
        _REFRESH_RATES[ip] = deque([now])
        return True
    window = _REFRESH_RATES[ip]
    while window and now - window[0] > _REFRESH_WINDOW:
        window.popleft()
    if len(window) >= _REFRESH_MAX:
        return False
    window.append(now)
    return True


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
    row = (await session.execute(
        select(HelixSettingModel.value).where(HelixSettingModel.key == "token_expiry_days")
    )).scalar_one_or_none()
    try:
        return int(row) if row else get_settings().token_expiry_days
    except (ValueError, TypeError):
        return get_settings().token_expiry_days

async def _make_tokens(
    user: HelixUserModel, session: AsyncSession,
    device_id: uuid.UUID | None = None,
) -> tuple[str, str, int]:
    """Issue a short-lived access token and a rotating refresh token.

    Returns (access_token, raw_refresh_token, expires_in_seconds).
    Persists the hashed refresh token to DB and commits. device_id binds
    the refresh chain to an auth_devices row (Group J); None only for
    legacy callers, which keep pre-J behavior.
    """
    settings    = get_settings()
    refresh_days = await _get_token_expiry_days(session)
    jti         = str(uuid.uuid4())

    access_token = create_dev_token(
        user_id=str(user.id),
        username=user.username,
        roles=list(user.roles or []),
        secret=settings.auth_secret,
        expire_minutes=settings.access_token_expiry_minutes,
        private_key=settings.auth_rsa_private_key or "",
        jti=jti,
    )

    raw_refresh  = secrets.token_urlsafe(48)
    token_hash   = hashlib.sha256(raw_refresh.encode()).hexdigest()
    expires_at   = datetime.now(timezone.utc) + timedelta(days=refresh_days)

    session.add(RefreshTokenModel(
        token_hash=token_hash,
        user_id=str(user.id),
        jti=jti,
        expires_at=expires_at,
        device_id=device_id,
    ))

    # Prune expired tokens for this user (bounded per-user housekeeping).
    # func.now() is dialect-portable (NOW() on PG/MySQL, CURRENT_TIMESTAMP on SQLite).
    await session.execute(
        delete(RefreshTokenModel).where(
            RefreshTokenModel.user_id == str(user.id),
            RefreshTokenModel.expires_at < func.now(),
        )
    )

    await session.commit()

    return access_token, raw_refresh, settings.access_token_expiry_minutes * 60

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
    device_id: Optional[str] = Field(None, max_length=36)  # Group J: reuse this browser's device row

class LoginResponse(BaseModel):
    access_token:  str
    refresh_token: str = ""
    token_type:    str = "bearer"
    expires_in:    int = 0      # seconds until access token expires
    user:          dict
    mfa_required:  bool = False
    device_id:     str = ""     # Group J: the auth_devices row this session is bound to

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

@router.post("/login", response_model=LoginResponse, dependencies=[Depends(_login_rl)])
async def login_real(
    request: Request,
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

    # Group J: bind this session's refresh chain to a device record
    from case_service.auth.devices import get_or_create_device
    device = await get_or_create_device(
        session, str(user.id),
        request.headers.get("user-agent", ""),
        request.client.host if request.client else None,
        claimed_device_id=body.device_id,
    )

    access_token, refresh_token, expires_in = await _make_tokens(
        user, session, device_id=device.id,
    )
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=_user_dict(user),
        device_id=str(device.id),
    )


# ── Register ──────────────────────────────────────────────────────

@router.post("/register", status_code=201, dependencies=[Depends(_register_rl)])
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

@router.post("/forgot-password", dependencies=[Depends(_forgot_rl)])
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


@router.post("/reset-password", dependencies=[Depends(_reset_rl)])
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
    device_id:    Optional[str] = Field(None, max_length=36)  # Group J


@router.post("/sso/callback")
async def sso_callback(
    request: Request,
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

    # Group J: SSO sessions are device-bound too
    from case_service.auth.devices import get_or_create_device
    device = await get_or_create_device(
        session, str(u.id),
        request.headers.get("user-agent", ""),
        request.client.host if request.client else None,
        claimed_device_id=body.device_id,
    )

    access_token, refresh_token, expires_in = await _make_tokens(
        u, session, device_id=device.id,
    )
    return {"access_token": access_token, "refresh_token": refresh_token,
            "expires_in": expires_in, "token_type": "bearer",
            "user": _user_dict(u), "device_id": str(device.id)}


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
    # Get-or-create via the ORM (dialect-portable; admin-only/low-concurrency, so
    # last-write-wins is fine). updated_at is handled by the model's default/onupdate.
    setting = await session.get(HelixSettingModel, "token_expiry_days")
    if setting is None:
        session.add(HelixSettingModel(
            key="token_expiry_days",
            value=str(body.token_expiry_days),
            updated_by=user.username,
        ))
    else:
        setting.value = str(body.token_expiry_days)
        setting.updated_by = user.username
    await session.commit()
    return {"token_expiry_days": body.token_expiry_days}


# ── Refresh token ─────────────────────────────────────────────────

class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., max_length=256)

@router.post("/refresh")
async def refresh_token_endpoint(
    request: Request,
    body:    RefreshRequest,
    session: AsyncSession = Depends(get_session),
):
    """Exchange a valid refresh token for a new access token + rotated refresh token."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_refresh_rate(client_ip):
        raise HTTPException(429, "Too many refresh attempts. Please wait before retrying.")

    token_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()

    # Atomic check-and-revoke: only one concurrent request can win this UPDATE.
    # If two requests race with the same token, the loser blocks on the row
    # write-lock, re-evaluates `revoked_at IS NULL` after the winner commits and
    # gets rowcount=0 → 401. Atomicity lives in the WHERE + rowcount, not in
    # RETURNING (which MySQL lacks); we re-SELECT the row we just locked —
    # token_hash is the PK and we hold the write-lock in this txn, so the SELECT
    # is guaranteed to return our row (read-your-writes). Dialect-portable.
    result = await session.execute(
        update(RefreshTokenModel)
        .where(
            RefreshTokenModel.token_hash == token_hash,
            RefreshTokenModel.revoked_at.is_(None),
            RefreshTokenModel.expires_at > func.now(),
        )
        .values(revoked_at=func.now(), revoked_by="rotation")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(401, "Invalid or expired refresh token.")

    row = (await session.execute(
        select(RefreshTokenModel.user_id, RefreshTokenModel.device_id)
        .where(RefreshTokenModel.token_hash == token_hash)
    )).one()

    # Group J: validate the device the chain is bound to. A revoked device or
    # a user-agent mismatch (token replayed from different software) kills
    # the whole chain — re-challenge with full credentials.
    from case_service.auth.devices import check_device_on_refresh
    device_id = uuid.UUID(str(row[1])) if row[1] else None
    if not await check_device_on_refresh(
        session, device_id,
        request.headers.get("user-agent", ""),
        client_ip,
    ):
        await session.commit()  # persist the chain revocation
        raise HTTPException(401, "Session device check failed. Please log in again.")

    user = await session.get(HelixUserModel, uuid.UUID(row[0]))
    if not user or not user.is_active:
        raise HTTPException(401, "Account is disabled.")

    access_token, new_refresh, expires_in = await _make_tokens(
        user, session, device_id=device_id,
    )
    return {
        "access_token":  access_token,
        "refresh_token": new_refresh,
        "token_type":    "bearer",
        "expires_in":    expires_in,
    }


# ── Logout ────────────────────────────────────────────────────────

class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None

@router.post("/logout")
async def logout_endpoint(
    body:        LogoutRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    session:     AsyncSession = Depends(get_session),
):
    """Revoke the current refresh token and add the access token to the revocation list."""
    settings = get_settings()

    # Revoke refresh token
    if body.refresh_token:
        token_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()
        rec = await session.get(RefreshTokenModel, token_hash)
        if rec and not rec.revoked_at:
            rec.revoked_at = datetime.now(timezone.utc)
            rec.revoked_by = "logout"

    # Revoke access token so it can't be used for the remaining TTL
    if credentials:
        raw_hash   = hashlib.sha256(credentials.credentials.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.access_token_expiry_minutes + 1
        )
        existing = await session.get(RevokedSessionModel, raw_hash)
        if not existing:
            session.add(RevokedSessionModel(
                token_hash=raw_hash,
                user_id="logout",
                reason="logout",
                expires_at=expires_at,
            ))

    await session.commit()
    return {"ok": True}


# ── Group J: device sessions ──────────────────────────────────────

@router.get("/devices")
async def list_devices(
    user:    AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """The current user's active devices (sign-in sessions)."""
    from case_service.db.models import AuthDeviceModel

    rows = (await session.execute(
        select(AuthDeviceModel)
        .where(AuthDeviceModel.user_id == user.user_id)
        .where(AuthDeviceModel.revoked_at.is_(None))
        .order_by(AuthDeviceModel.last_seen_at.desc())
    )).scalars().all()
    return [
        {
            "id": str(d.id),
            "device_name": d.device_name,
            "last_ip": d.last_ip,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        }
        for d in rows
    ]


@router.delete("/devices/{device_id}")
async def revoke_device_endpoint(
    device_id: uuid.UUID,
    user:      AuthenticatedUser = Depends(get_current_user),
    session:   AsyncSession = Depends(get_session),
):
    """Sign out a device: revokes it and every refresh token bound to it."""
    from case_service.db.models import AuthDeviceModel
    from case_service.auth.devices import revoke_device

    dev = await session.get(AuthDeviceModel, device_id)
    # 404 for both missing and foreign devices — don't confirm existence
    if dev is None or (dev.user_id != user.user_id and "admin" not in (user.roles or [])):
        raise HTTPException(404, "Device not found")
    if dev.revoked_at is not None:
        return {"ok": True, "revoked_tokens": 0}

    count = await revoke_device(session, dev, revoked_by=user.user_id)
    try:
        from case_service.enterprise.security_events import log_security_event
        await log_security_event(
            session, event_type="auth.device_revoked", severity="info",
            user_id=user.user_id, resource_type="device", resource_id=str(device_id),
            action="revoke_device", outcome="success",
            details={"device_name": dev.device_name, "revoked_tokens": count},
        )
    except Exception:
        pass
    await session.commit()
    return {"ok": True, "revoked_tokens": count}


# ── Group J: WebAuthn passkeys ────────────────────────────────────

class WebAuthnVerifyBody(BaseModel):
    credential:  dict
    device_name: Optional[str] = Field(None, max_length=255)


@router.post("/webauthn/register/options")
async def webauthn_register_options(
    user:     AuthenticatedUser = Depends(get_current_user),
    session:  AsyncSession = Depends(get_session),
):
    """Begin passkey enrollment for the logged-in user."""
    from case_service.auth import webauthn_service

    options_json = await webauthn_service.begin_registration(
        session, user.user_id, user.username,
        getattr(user, "display_name", "") or user.username,
    )
    await session.commit()
    # options_to_json already produced spec-compliant JSON — pass through raw
    return Response(content=options_json, media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@router.post("/webauthn/register/verify")
async def webauthn_register_verify(
    body:    WebAuthnVerifyBody,
    user:    AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Complete passkey enrollment: verify attestation, store the public key."""
    from case_service.auth import webauthn_service

    try:
        cred = await webauthn_service.complete_registration(
            session, user.user_id, body.credential, body.device_name or "Passkey",
        )
    except Exception as e:
        raise HTTPException(400, f"Passkey registration failed: {e}")

    try:
        from case_service.enterprise.security_events import log_security_event
        await log_security_event(
            session, event_type="auth.passkey_registered", severity="info",
            user_id=user.user_id, resource_type="passkey", resource_id=str(cred.id),
            action="register_passkey", outcome="success",
            details={"device_name": cred.device_name},
        )
    except Exception:
        pass
    await session.commit()
    return {"ok": True, "id": str(cred.id), "device_name": cred.device_name}


@router.get("/webauthn/credentials")
async def webauthn_list_credentials(
    user:    AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """The current user's registered passkeys."""
    from case_service.db.models import WebAuthnCredentialModel

    rows = (await session.execute(
        select(WebAuthnCredentialModel)
        .where(WebAuthnCredentialModel.user_id == user.user_id)
        .where(WebAuthnCredentialModel.revoked_at.is_(None))
        .order_by(WebAuthnCredentialModel.created_at.desc())
    )).scalars().all()
    return [
        {
            "id": str(c.id),
            "device_name": c.device_name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        }
        for c in rows
    ]


@router.delete("/webauthn/credentials/{credential_id}")
async def webauthn_delete_credential(
    credential_id: uuid.UUID,
    user:    AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Remove a passkey."""
    from case_service.db.models import WebAuthnCredentialModel

    cred = await session.get(WebAuthnCredentialModel, credential_id)
    # 404 for both missing and foreign credentials — don't confirm existence
    if cred is None or cred.user_id != user.user_id:
        raise HTTPException(404, "Passkey not found")
    cred.revoked_at = datetime.now(timezone.utc)
    try:
        from case_service.enterprise.security_events import log_security_event
        await log_security_event(
            session, event_type="auth.passkey_removed", severity="info",
            user_id=user.user_id, resource_type="passkey", resource_id=str(credential_id),
            action="remove_passkey", outcome="success",
            details={"device_name": cred.device_name},
        )
    except Exception:
        pass
    await session.commit()
    return {"ok": True}


# ── Group J: passkey login ────────────────────────────────────────

class WebAuthnLoginOptionsBody(BaseModel):
    username: Optional[str] = Field(None, max_length=50)


@router.post("/webauthn/login/options", dependencies=[Depends(_login_rl)])
async def webauthn_login_options(
    body:    WebAuthnLoginOptionsBody,
    session: AsyncSession = Depends(get_session),
):
    """Begin passkey login. Anonymous; rate-limited like password login.

    Unknown usernames and accounts without passkeys get the same
    discoverable-credential options as everyone else — the response shape
    never confirms whether an account exists (anti-enumeration).
    """
    from case_service.auth import webauthn_service

    user_id: str | None = None
    if body.username:
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.username == body.username.strip())
        )).scalar_one_or_none()
        if u is not None:
            user_id = str(u.id)

    try:
        options_json = await webauthn_service.begin_authentication(session, user_id)
    except ValueError:
        # account exists but has no passkeys — indistinguishable from unknown
        options_json = await webauthn_service.begin_authentication(session, None)
    await session.commit()
    return Response(content=options_json, media_type="application/json",
                    headers={"Cache-Control": "no-store"})


class WebAuthnLoginVerifyBody(BaseModel):
    credential: dict
    device_id:  Optional[str] = Field(None, max_length=36)


@router.post("/webauthn/login/verify", response_model=LoginResponse, dependencies=[Depends(_login_rl)])
async def webauthn_login_verify(
    request: Request,
    body:    WebAuthnLoginVerifyBody,
    session: AsyncSession = Depends(get_session),
):
    """Complete passkey login: verify the assertion, issue the token pair.

    A verified passkey satisfies MFA — it is possession + on-device user
    verification, and phishing-resistant where TOTP is not — so no TOTP
    stage follows.
    """
    from case_service.auth import webauthn_service
    from case_service.auth.devices import get_or_create_device

    try:
        cred, user_verified = await webauthn_service.complete_authentication(
            session, body.credential, "login",
        )
    except Exception as e:
        try:
            from case_service.enterprise.security_events import log_security_event
            await log_security_event(
                session, event_type="auth.login_failed", severity="warning",
                user_id="unknown", resource_type="user", resource_id="unknown",
                action="passkey_login", outcome="denied", details={"reason": str(e)},
            )
            await session.commit()
        except Exception:
            pass
        raise HTTPException(401, "Passkey sign-in failed.")

    user = await session.get(HelixUserModel, uuid.UUID(cred.user_id))
    if not user or not user.is_active:
        raise HTTPException(403, "Account is disabled. Contact an administrator.")
    if _is_locked(user):
        remaining = int((user.locked_until - datetime.now(timezone.utc)).total_seconds() / 60) + 1
        raise HTTPException(423, f"Account locked. Try again in {remaining} minute(s).")

    # A TOTP-enrolled account's bar is two factors. A possession-only
    # assertion (UP without UV) must not lower it — require the
    # authenticator to have verified the user (biometric/PIN).
    if user.mfa_enabled and not user_verified:
        raise HTTPException(
            401,
            "This account requires user verification — use a passkey with "
            "biometrics or a PIN, or sign in with password + MFA code.",
        )

    user.failed_attempts = 0
    user.locked_until    = None
    user.last_login_at   = datetime.now(timezone.utc)

    try:
        from case_service.enterprise.security_events import log_security_event
        from case_service.hxstream.emitter import emit_trace
        await log_security_event(
            session, event_type="auth.login", severity="info",
            user_id=user.username, resource_type="user", resource_id=user.username,
            action="login", outcome="success",
            details={"username": user.username, "method": "passkey"},
        )
        await emit_trace("auth.login", {"username": user.username, "method": "passkey"},
                         actor_user_id=user.username, session=session)
    except Exception:
        pass

    device = await get_or_create_device(
        session, str(user.id),
        request.headers.get("user-agent", ""),
        request.client.host if request.client else None,
        claimed_device_id=body.device_id,
    )
    access_token, refresh_token, expires_in = await _make_tokens(
        user, session, device_id=device.id,
    )
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=_user_dict(user),
        device_id=str(device.id),
    )


@router.post("/webauthn/stepup/options")
async def webauthn_stepup_options(
    user:    AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Begin a step-up assertion for the logged-in user (PUO approvals etc.)."""
    from case_service.auth import webauthn_service

    try:
        options_json = await webauthn_service.begin_authentication(
            session, user.user_id, purpose="stepup",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await session.commit()
    return Response(content=options_json, media_type="application/json",
                    headers={"Cache-Control": "no-store"})
