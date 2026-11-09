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

import asyncio
import hashlib
import logging
import hmac
import secrets
import time as _time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import HelixUserModel, AuthOtpModel, SsoProviderModel, RefreshTokenModel, RevokedSessionModel, HelixSettingModel
from velaris_shared import hxguard
from velaris_shared.db.session import get_auth_session as get_session
from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared.tenancy.identity import tenant_for_write
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.hxbridge.encryption import encrypt_credentials, decrypt_credentials, encrypt_credentials_vaulted

_bearer = HTTPBearer(auto_error=False)

log = logging.getLogger("user_service.auth_real")

router = APIRouter(prefix="/auth/real", tags=["auth-real"])

MAX_ATTEMPTS  = 5
LOCKOUT_MINS  = 15
OTP_VALID_MINS = 10

# ── D4: per-IP rate limits on credential endpoints ────────────────────────────
# Complements the per-user lockout (MAX_ATTEMPTS): the lockout stops attacks
# on one account; these stop one IP from spraying across many accounts.
from velaris_shared.middleware.endpoint_rate_limit import rate_limit
from velaris_shared.core.error_codes import (
    AUTH_CREDENTIALS_401,
    AUTH_DEVICE_MISMATCH_401,
    AUTH_DISABLED_403,
    AUTH_EMAIL_TAKEN_409,
    AUTH_OTP_INVALID_400,
    AUTH_RATE_LIMIT_429,
    AUTH_SSO_PROVISION_403,
    AUTH_SSO_REQUIRED_400,
    AUTH_SSO_STATE_400,
    AUTH_SSO_UNCONFIGURED_404,
    AUTH_SSO_UNVERIFIED_403,
    coded,
)

_login_rl    = rate_limit(max_calls=10, window_seconds=60,  name="login")
_register_rl = rate_limit(max_calls=10, window_seconds=300, name="registration")
_forgot_rl   = rate_limit(max_calls=5,  window_seconds=300, name="password-reset")
# The SSO endpoints are unauthenticated like the ones above, and the callback
# additionally makes two to three outbound calls per request — so without this
# it is both brute-force surface and an amplifier pointed at the provider.
_sso_rl      = rate_limit(max_calls=20, window_seconds=60,  name="sso")
_reset_rl    = rate_limit(max_calls=5,  window_seconds=300, name="password-reset")

# ── Refresh-endpoint rate limiter (sliding window, shared when the cache is on) ──
# Bounds: 10 requests per IP per 60 s. Memory: O(unique_IPs * 10 * 8 bytes).
# Multi-worker deployments multiply this by worker count — still safe given
# the 288-bit token entropy makes brute-force infeasible at any rate.
_REFRESH_MAX    = 10
_REFRESH_WINDOW = 60.0   # seconds


async def _check_refresh_rate(ip: str) -> bool:
    """Return False when the IP has exceeded _REFRESH_MAX calls in _REFRESH_WINDOW s.

    Shared by every copy of the service when the cache is on (R5e step 5), so N
    copies are not N times the limit; cache off, this process's own window.
    """
    from velaris_shared import shared_state
    return await shared_state.allow("auth-refresh", ip, _REFRESH_MAX, _REFRESH_WINDOW)


# ── Password helpers ──────────────────────────────────────────────

# One implementation for every service (R5e step 10a): velaris_shared.auth.passwords.
from velaris_shared.auth.passwords import hash_password as _hash_password  # noqa: E402
from velaris_shared.auth.passwords import verify_password as _verify_password  # noqa: E402
# In the routes, the async forms: bcrypt on a worker thread, not the event loop.
from velaris_shared.auth.passwords import hash_password_async as _hash_password_a  # noqa: E402
from velaris_shared.auth.passwords import verify_password_async as _verify_password_a  # noqa: E402

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
        # Deleted (mig 223): refused everywhere, purged after `purge_after`
        # unless an administrator undoes it. None = not deleted.
        "deleted_at":   user.deleted_at.isoformat() if user.deleted_at else None,
        "purge_after":  user.purge_after.isoformat() if user.purge_after else None,
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

    # A deleted account (mig 223) answers like a wrong password: nothing to learn.
    if not user or user.deleted_at is not None:
        raise coded(401, AUTH_CREDENTIALS_401, "Invalid username or password.")

    if not user.is_active:
        raise coded(403, AUTH_DISABLED_403, "Account is disabled. Contact an administrator.")

    if _is_locked(user):
        remaining = int((user.locked_until - datetime.now(timezone.utc)).total_seconds() / 60) + 1
        raise HTTPException(423, f"Account locked. Try again in {remaining} minute(s).")

    if not user.password_hash:
        raise coded(400, AUTH_SSO_REQUIRED_400, "This account uses SSO login. Use the SSO button instead.")

    if not await _verify_password_a(body.password, user.password_hash):
        user.failed_attempts += 1
        locked_now = user.failed_attempts >= MAX_ATTEMPTS
        if locked_now:
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINS)
            user.failed_attempts = 0
        await session.commit()
        # Audit: failed login
        try:
            from velaris_shared.db.security_events import log_security_event
            from velaris_shared.hxstream.emitter import emit_trace
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
                from velaris_shared.db.security_events import log_security_event
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
        from velaris_shared.db.security_events import log_security_event
        from velaris_shared.hxstream.emitter import emit_trace
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
    from velaris_shared.auth.devices import get_or_create_device
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
    user:    AuthenticatedUser = Depends(hxguard.guard("identity.users.write")),
):
    """Create a new Velaris user (auth account). Admin only."""
    existing = (await session.execute(
        select(HelixUserModel).where(
            (HelixUserModel.username == body.username) | (HelixUserModel.email == body.email)
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Username or email already in use.")
    _assert_role_grant(user, body.roles)

    new_user = HelixUserModel(
        username=body.username,
        email=body.email,
        display_name=body.display_name,
        password_hash=await _hash_password_a(body.password),
        roles=body.roles,
        password_change_required=body.password_change_required,
    )
    session.add(new_user)
    await session.flush()
    await place_in_callers_tenant(session, user, new_user)
    await session.commit()
    await session.refresh(new_user)
    return _user_dict(new_user)


# ── Superadmin guard ──────────────────────────────────────────────


# Moved to the shared layer (R5e step 10a): hxdraft creates accounts too.
from velaris_shared.tenancy.placement import place_in_callers_tenant  # noqa: E402,F401


def _assert_role_grant(actor: AuthenticatedUser, requested: list[str],
                       held: list[str] | None = None) -> None:
    """Only a superadmin may grant `superadmin`.

    Until 2026-09-29 `roles` was taken verbatim on register and on PATCH, and
    login copies it into the token — so any holder of `identity.users.write`,
    every tenant's admin, could make themselves or a new account superadmin:
    the one role that crosses every tenant boundary. Compared case-insensitively
    because the role checks elsewhere are.
    """
    wants = {str(r).strip().lower() for r in requested or []}
    had = {str(r).strip().lower() for r in held or []}
    if "superadmin" in wants and "superadmin" not in had and not _is_superadmin_user(actor):
        raise HTTPException(403, "Only a superadmin may grant the superadmin role.")


async def _target_in_reach(session: AsyncSession, actor: AuthenticatedUser,
                           target: HelixUserModel) -> None:
    """A tenant's admin manages their own tenant's accounts; the install's
    operator manages all. An account's tenant is its directory row's (by id or
    username) — none, or NULL, is the platform's default tenant (8d)."""
    from velaris_shared.tenancy.identity import (
        DEFAULT_TENANT, is_platform_operator, same_tenant,
    )
    from velaris_shared.db.models import UserDirectoryModel

    if await is_platform_operator(session, actor):
        return
    row = (await session.execute(select(UserDirectoryModel).where(
        UserDirectoryModel.user_id.in_([str(target.id), target.username])))).scalars().first()
    owner = (row.tenant_id if row is not None else None) or DEFAULT_TENANT
    if not await same_tenant(session, owner, getattr(actor, "tenant_id", None)):
        raise HTTPException(404, "User not found")

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
    if not (hxguard.user_allows(actor, "identity.users.write") or _is_superadmin_user(actor)):
        raise HTTPException(403, "Only admins can modify user accounts.")


# ── User management ───────────────────────────────────────────────

@router.get("/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(hxguard.guard("identity.users.read")),
):
    # Superadmin is hidden from all user management views
    rows = (await session.execute(
        select(HelixUserModel)
        .where(HelixUserModel.is_superadmin == False)  # noqa: E712
        .order_by(HelixUserModel.username)
    )).scalars().all()
    # Every tenant's users, to every tenant's admin, until 2026-09-29 (8d). A
    # user's tenant is their directory row's (by id or username); one with no
    # row is the platform's and shown to its operator only.
    from velaris_shared.tenancy.identity import is_platform_operator, same_tenant

    if not await is_platform_operator(session, user):
        from velaris_shared.db.models import UserDirectoryModel
        dirs = {d.user_id: d.tenant_id for d in (await session.execute(
            select(UserDirectoryModel))).scalars().all()}
        token = getattr(user, "tenant_id", None)
        mine = []
        for r in rows:
            owner = dirs.get(str(r.id), dirs.get(r.username))
            if owner is not None and await same_tenant(session, owner, token):
                mine.append(r)
        rows = mine
    return {"users": [_user_dict(r) for r in rows], "total": len(rows)}


@router.get("/users/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(hxguard.guard("identity.users.read")),
):
    u = await session.get(HelixUserModel, user_id)
    if not u or getattr(u, "is_superadmin", False):
        raise HTTPException(404, "User not found")
    await _target_in_reach(session, user, u)
    return _user_dict(u)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    body:    UpdateUserRequest,
    session: AsyncSession = Depends(get_session),
    actor:   AuthenticatedUser = Depends(hxguard.guard("identity.users.write")),
):
    u = await session.get(HelixUserModel, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    await _target_in_reach(session, actor, u)
    _assert_can_modify(actor, u)
    if u.deleted_at is not None:
        # Only an undo brings a deleted account back (POST …/restore).
        raise HTTPException(409, "This account is deleted — restore it first.")
    if body.roles is not None:
        _assert_role_grant(actor, body.roles, u.roles)

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
    permanent: bool = False,
    session: AsyncSession = Depends(get_session),
    actor:   AuthenticatedUser = Depends(hxguard.guard("identity.users.write")),
):
    """Delete an account (Nov_TODO "User deletion v2"). Refused everywhere and
    signed out at once; kept — username reserved — for the grace period
    (`VELARIS_CASE_USER_DELETE_GRACE_DAYS`) so an administrator can undo it, then
    purged by the worker. `permanent=true`: purged now (e.g. a compromised
    account) — or, on an account already deleted, purged ahead of its date."""
    from velaris_shared.config import get_settings
    from velaris_shared.db.security_events import log_security_event
    from velaris_shared.identity import lifecycle

    u = await session.get(HelixUserModel, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    await _target_in_reach(session, actor, u)
    _assert_can_modify(actor, u)
    if u.deleted_at is not None and not permanent:
        return None                                   # already deleted: nothing more to do
    try:
        if u.deleted_at is None:
            await lifecycle.check_delete_allowed(session, actor, u)
    except lifecycle.DeleteRefused as exc:
        raise HTTPException(409, str(exc)) from None
    recent = await lifecycle.deletes_by(session, actor, hours=1)
    uid, name = str(u.id), u.username
    if permanent:
        removed = await lifecycle.purge_user(session, u, actor=actor.username, actor_id=actor.user_id)
        kind = "user_purged"
    else:
        removed = await lifecycle.mark_deleted(session, u, actor=actor.username, actor_id=actor.user_id)
        kind = "user_deleted"
    s = get_settings()
    await log_security_event(
        session, event_type=kind, severity="high", user_id=actor.user_id,
        resource_type="user", resource_id=uid, action="delete", outcome="success",
        details={"username": name, "permanent": permanent,
                 "purge_after": None if permanent else u.purge_after.isoformat(),
                 "removed": {k: v for k, v in removed.items() if v}},
    )
    if kind == "user_deleted" and recent + 1 > s.user_delete_bulk_alert:
        # One administrator deleting many accounts: worth a look at once.
        await log_security_event(
            session, event_type="user_delete_bulk", severity="critical", user_id=actor.user_id,
            resource_type="user", resource_id=uid, action="delete", outcome="success",
            details={"deleted_in_last_hour": recent + 1, "alert_at": s.user_delete_bulk_alert,
                     "refused_at": s.user_delete_bulk_limit},
        )
    await session.commit()
    if permanent:
        # The ledger file at once (queued in the purge's transaction above);
        # a failure is reported and retried by the hourly job.
        from velaris_shared.identity import ledger
        await ledger.flush(session)
    return None


@router.post("/users/{user_id}/restore")
async def restore_deleted_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor:   AuthenticatedUser = Depends(hxguard.guard("identity.users.write")),
):
    """Undo a delete within its grace period. The account comes back as it was;
    the user signs in again (sessions and devices ended by the delete stay
    ended)."""
    from velaris_shared.db.security_events import log_security_event
    from velaris_shared.identity import lifecycle

    u = await session.get(HelixUserModel, user_id)
    if not u:
        raise HTTPException(404, "User not found")         # purged: nothing to undo
    await _target_in_reach(session, actor, u)
    _assert_can_modify(actor, u)
    try:
        await lifecycle.undo_delete(session, u, actor=actor.username)
    except lifecycle.DeleteRefused as exc:
        raise HTTPException(409, str(exc)) from None
    await log_security_event(
        session, event_type="user_delete_undone", severity="high", user_id=actor.user_id,
        resource_type="user", resource_id=str(u.id), action="restore", outcome="success",
        details={"username": u.username},
    )
    await session.commit()
    return _user_dict(u)


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
            raise coded(409, AUTH_EMAIL_TAKEN_409, "That email is already in use by another account.")
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
    if not await _verify_password_a(body.current_password, u.password_hash):
        raise HTTPException(401, "Current password is incorrect.")
    u.password_hash = await _hash_password_a(body.new_password)
    u.password_change_required = False
    u.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


# ── Forgot / reset password ───────────────────────────────────────

#: One answer for every outcome of `POST /forgot-password`: unknown address,
#: federated account, real account. It has to mention SSO, because a federated
#: user gets no code and would otherwise sit waiting for an email — but it must
#: say so to *everybody*, or the wording itself discloses which addresses exist
#: and which are SSO-only.
_RESET_UNIFORM_MESSAGE = (
    "If an account exists for this address, a code has been sent. If your "
    "organisation uses single sign-on, sign in with your provider instead."
)


#: Reset mails still being handed to case-service. Held so a running task is not
#: garbage-collected; tests await them.
_pending_mail: set = set()


async def _send_reset_code(to: str, code: str) -> None:
    """Ask case-service to mail the code — detached from the request.

    Inline, the answer for a real account waited on an internal call and an SMTP
    send (seconds) while an unknown address answered at once: the timing said
    which addresses have an account, which the uniform message exists to hide.
    Not a FastAPI background task either: behind this app's middleware those
    still finish before the last byte of the response is sent. Own session: the
    request's is closed by the time this runs. A failure is logged, never
    raised."""
    try:
        from velaris_shared.db.session import get_session_factory
        from velaris_shared.internal import client as internal
        async with get_session_factory()() as s:
            await internal.call("user/otp-email", {
                "purpose": "password_reset", "to": to, "code": code,
                "valid_minutes": OTP_VALID_MINS}, session=s)
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("password-reset email not sent: %s", type(exc).__name__)


@router.post("/forgot-password", dependencies=[Depends(_forgot_rl)])
async def forgot_password(
    body:    ForgotPasswordRequest,
    session: AsyncSession = Depends(get_session),
):
    """Send a 6-digit OTP to the user's email for password reset.

    **A federated account gets no code.** Reset is the one flow that hands out a
    local password without asking for the old one, so leaving it open lets an
    SSO user mint a credential they never had — and that credential outlives
    their IdP account. Revoking them at the identity provider would then stop
    the SSO button working and change nothing else, which is precisely the
    leaver case the provider is supposed to close. Change-password is not the
    hole; it already requires a password the account does not have.

    "Federated" is `password_hash IS NULL`: `create_user` always sets one, and
    the JIT spine always writes `None`, so the column is the fact rather than a
    second flag that can disagree with it.
    """
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.email == body.email)
    )).scalar_one_or_none()
    # Always return 200 to avoid email enumeration — and spend the same bcrypt
    # round a real account spends hashing its code, or the time answers instead.
    # A deleted account (mig 223) is no account: no code is sent.
    if not u or u.deleted_at is not None:
        await _hash_password_a(secrets.token_hex(3))
        return {"ok": True, "message": _RESET_UNIFORM_MESSAGE}

    if not u.password_hash:
        # Identical response and identical next screen — the client still walks
        # on to "enter your code", where any code fails the same way an expired
        # one does. Refusing visibly here, or skipping that step, would make the
        # *flow* the oracle that the message was carefully written not to be.
        from velaris_shared.db.security_events import log_security_event
        await log_security_event(
            session, event_type="password_reset_refused_federated", severity="low",
            resource_type="user", resource_id=str(u.id),
            action="forgot_password", outcome="denied",
            details={"reason": "no_local_password"},
        )
        await session.commit()
        return {"ok": True, "message": _RESET_UNIFORM_MESSAGE}

    otp_plain = "".join([str(secrets.randbelow(10)) for _ in range(6)])
    otp_rec = AuthOtpModel(
        user_id=u.id,
        otp_hash=await _hash_password_a(otp_plain),
        purpose="password_reset",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_VALID_MINS),
    )
    session.add(otp_rec)
    await session.commit()

    # case-service composes and sends it (R5e step 10, `/internal/v1/user/otp-email`),
    # detached from the response (`_send_reset_code`). Until then this imported a
    # `MailService` that never existed, and a bare `except: pass` hid it: no
    # reset code was ever emailed. The answer stays the same uniform one.
    task = asyncio.create_task(_send_reset_code(u.email, otp_plain))
    _pending_mail.add(task)
    task.add_done_callback(_pending_mail.discard)

    return {"ok": True, "message": _RESET_UNIFORM_MESSAGE}


@router.post("/reset-password", dependencies=[Depends(_reset_rl)])
async def reset_password(
    body:    ResetPasswordRequest,
    session: AsyncSession = Depends(get_session),
):
    u = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.email == body.email)
    )).scalar_one_or_none()
    if not u or u.deleted_at is not None:
        raise coded(400, AUTH_OTP_INVALID_400, "Invalid or expired OTP.")

    # Checked here as well as at `/forgot-password`, and not as tidiness: an OTP
    # minted while the account still had a password stays valid for its window,
    # so an account federated (or stripped of its password) in between could
    # have its credential restored by a code that predates the change. Same
    # refusal as a bad OTP — the anti-enumeration answer is the right one here
    # too.
    if not u.password_hash:
        raise coded(400, AUTH_OTP_INVALID_400, "Invalid or expired OTP.")

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

    if not otp_rec or not await _verify_password_a(body.otp, otp_rec.otp_hash):
        # Increment failure counter; lock and invalidate active OTPs after MAX_ATTEMPTS.
        u.failed_attempts += 1
        if u.failed_attempts >= MAX_ATTEMPTS:
            u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINS)
            u.failed_attempts = 0
            # Invalidate all pending reset OTPs so they can't be retried after unlock.
            if otp_rec:
                otp_rec.used_at = datetime.now(timezone.utc)
        await session.commit()
        raise coded(400, AUTH_OTP_INVALID_400, "Invalid or expired OTP.")

    u.password_hash = await _hash_password_a(body.new_password)
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
    u.mfa_secret_enc = await encrypt_credentials_vaulted(
        session, {"secret": secret}, tenant_id=getattr(u, "tenant_id", None))
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
):
    """Which SSO buttons to render. Deliberately anonymous.

    It used to require a bearer token, which no one has on a login page: the
    Studio and portal sign-in screens both got a 401 here and rendered zero SSO
    buttons, so the whole flow was unreachable. Nothing here is a secret — the
    provider name and `client_id` are both visible in the authorize URL the
    very next request builds. `tenant_id` is not returned, and was the only
    field an anonymous caller had no business seeing.
    """
    rows = (await session.execute(
        select(SsoProviderModel)
        .where(SsoProviderModel.enabled == True)  # noqa: E712
        .order_by(SsoProviderModel.provider)
    )).scalars().all()

    # The parameter was accepted and then ignored, so a portal asking for its
    # own providers was shown every tenant's. A NULL tenant_id stays visible to
    # everyone (same_tenant's rule for platform-shared rows).
    if tenant_id:
        from velaris_shared.tenancy.identity import same_tenant
        rows = [r for r in rows if await same_tenant(session, r.tenant_id, tenant_id)]

    def _login_url(row: SsoProviderModel) -> str | None:
        """Where a SAML button has to send the browser.

        SAML is not an OAuth redirect: the sign-in starts at
        `/auth/saml/{tenant}/login`, and the tenant is in the path. The segment
        is read out of the provider's configured **ACS URL** rather than from
        `tenant_id`, which this endpoint deliberately does not disclose — the
        ACS URL is already published in the SP metadata that the IdP
        administrator pastes into their console, so it is public by design,
        while `tenant_id` on an OAuth row is not.
        """
        if row.provider != "saml":
            return None
        from urllib.parse import urlsplit
        acs = str((row.config or {}).get("acs_url") or "")
        parts = [p for p in urlsplit(acs).path.split("/") if p]
        # …/auth/saml/{tenant}/acs
        if len(parts) < 2 or parts[-1] != "acs":
            return None
        return f"/api/v1/auth/saml/{parts[-2]}/login"

    def _branding(row: SsoProviderModel) -> dict:
        """What the sign-in button should say and show.

        Both come from the provider's `config` because the answer is per
        deployment: one customer's button says "Okta", another's says the name
        of their own identity team, and `saml` on its own tells a user nothing
        about which door they are being sent to. `icon_url` is a URL the
        administrator supplies — the IdP's official mark, downloaded from their
        brand page and served by us, or a `data:` URI. Nothing is a secret: the
        login page is public, and so is the fact that it offers SSO.
        """
        cfg = dict(row.config or {})
        out: dict[str, str] = {}
        name = str(cfg.get("display_name") or "").strip()
        if name:
            out["display_name"] = name
        icon = str(cfg.get("icon_url") or "").strip()
        # Same-origin paths and data URIs only. An arbitrary remote URL on the
        # sign-in page would tell a third party who is about to log in, and
        # would break the moment that host does.
        #
        # `startswith("/")` was not enough on its own: a browser normalises `\`
        # to `/` inside an http(s) URL and strips tab/newline before parsing, so
        # `/\evil.com/x.png` and `/\t/evil.com` are fetched from evil.com while
        # passing a leading-slash test. `is_safe_local_path` is the same rule the
        # RelayState prefixes use.
        from user_service.identity.saml import is_safe_local_path

        if icon.startswith("data:image/") or is_safe_local_path(icon):
            out["icon_url"] = icon
        return out

    return {
        "providers": [
            {"id": str(r.id), "provider": r.provider, "client_id": r.client_id,
             "enabled": r.enabled,
             **({"login_url": url} if (url := _login_url(r)) else {}),
             **_branding(r)}
            for r in rows
        ]
    }


#: The four endpoint keys a custom provider must supply on its `config`. The two
#: presets above exist because they are the providers with quirks worth encoding
#: (GitHub's separate verified-email endpoint); anything else is plain OIDC.
_CUSTOM_ENDPOINT_KEYS = ("auth_url", "token_url", "userinfo", "scope")


def _endpoints_for(p_row: SsoProviderModel) -> dict:
    """Resolve a provider row to its OAuth endpoints.

    A preset name wins, so an admin cannot repoint `google` at their own host by
    editing `config`. Everything else comes from the row, which is what makes
    the provider field free text rather than a two-item list.
    """
    if p_row.provider in OAUTH_ENDPOINTS:
        return OAUTH_ENDPOINTS[p_row.provider]
    cfg = dict(p_row.config or {})
    return {
        "auth_url":  str(cfg.get("auth_url") or ""),
        "token_url": str(cfg.get("token_url") or ""),
        "userinfo":  str(cfg.get("userinfo") or ""),
        "emails":    str(cfg.get("emails") or "") or None,
        "scope":     str(cfg.get("scope") or "openid email profile"),
    }


async def _assert_endpoints_safe(ep: dict) -> None:
    """Every URL we are about to call must survive the SSRF guard.

    Re-checked on the request path, not only on write: `config` is a plain JSON
    column that a DBA or a migration can change without going through the API.
    The client secret is POSTed to `token_url`, so a URL pointing at internal
    infrastructure would hand that secret to it.
    """
    from velaris_shared.hxbridge.security import validate_outbound_url

    for key in ("auth_url", "token_url", "userinfo", "emails"):
        url = ep.get(key)
        if not url:
            continue
        if not str(url).lower().startswith("https://"):
            raise HTTPException(400, f"SSO {key} must be an https:// URL.")
        try:
            await validate_outbound_url(str(url))
        except ValueError as exc:
            raise HTTPException(400, f"SSO {key} is not allowed: {exc}") from exc


async def _validated_custom_provider(provider: str, config: dict) -> None:
    """A non-preset provider is only usable once it carries its own endpoints."""
    if provider in OAUTH_ENDPOINTS:
        return
    if provider == "saml":
        # SAML rows live in this table for the tenant scoping, enable flag and
        # admin CRUD, but they are not OAuth: there is no token_url to call and
        # no client secret to send. `identity.saml.SamlConfig.missing()` is what
        # validates them, on the request path where the config is used.
        from user_service.identity.saml import (
            MAX_CLOCK_SKEW_SECONDS, SamlConfig, is_safe_local_path,
        )

        gaps = SamlConfig.from_row(config).missing()
        if gaps:
            raise HTTPException(
                400,
                "A SAML provider needs its IdP details before it can be used. "
                f"Missing: {', '.join(gaps)}.",
            )
        # `from_row` drops an unsafe prefix, so a provider saved with one would
        # simply behave differently from what the admin typed. Refused here so
        # the answer is "no", not a silent edit.
        unsafe = [
            str(p) for p in (config.get("allowed_relay_state_prefixes") or [])
            if not is_safe_local_path(str(p))
        ]
        if unsafe:
            raise HTTPException(
                400,
                "A landing path must be a path on this server, starting with a single "
                f"'/' and containing no backslash or whitespace. Refused: {', '.join(unsafe)}.",
            )
        metadata_url = str(config.get("idp_metadata_url") or "").strip()
        if metadata_url:
            # Checked on write as well as before every scheduled fetch: a URL
            # this server will GET on a timer is an SSRF primitive if it is only
            # ever validated in one of those places.
            if not metadata_url.lower().startswith("https://"):
                raise HTTPException(400, "The metadata URL must be https://.")
            try:
                await validate_outbound_url(metadata_url)
            except ValueError as exc:
                raise HTTPException(400, f"The metadata URL is not allowed: {exc}") from exc

        skew = config.get("clock_skew_seconds")
        if skew is not None:
            try:
                skew_int = int(skew)
            except (TypeError, ValueError):
                raise HTTPException(400, "clock_skew_seconds must be a whole number of seconds.") from None
            if not 0 <= skew_int <= MAX_CLOCK_SKEW_SECONDS:
                raise HTTPException(
                    400,
                    f"clock_skew_seconds must be between 0 and {MAX_CLOCK_SKEW_SECONDS}. "
                    "A larger tolerance stops NotOnOrAfter meaning anything.",
                )
        return
    missing = [k for k in _CUSTOM_ENDPOINT_KEYS if not str(config.get(k) or "").strip()]
    if missing:
        raise HTTPException(
            400,
            f"'{provider}' is not a built-in provider, so its OAuth endpoints must be "
            f"supplied. Missing: {', '.join(missing)}.",
        )
    await _assert_endpoints_safe({
        "auth_url":  config.get("auth_url"),
        "token_url": config.get("token_url"),
        "userinfo":  config.get("userinfo"),
        "emails":    config.get("emails"),
    })


def _validated_provider_config(config: dict | None) -> dict:
    """Reject a provisioning config that would mint privileged accounts.

    `default_roles` is applied to accounts created by *strangers* completing an
    OAuth flow, so `superadmin` there would turn "anyone at an allowed domain"
    into a system account that `_assert_can_modify` then refuses to touch. An
    admin has other, audited ways to grant roles; this is not one of them.
    """
    cfg = dict(config or {})
    roles = [str(r) for r in (cfg.get("default_roles") or [])]
    if any(r.strip().lower() == "superadmin" for r in roles):
        raise HTTPException(
            400, "default_roles cannot include 'superadmin' — an auto-provisioned "
                 "account must never be a system account.",
        )
    # The sign-in page is public and unauthenticated, so an icon it fetches from
    # a third party reports every visitor to that host. `_branding` drops one it
    # cannot vouch for; refusing here means the admin finds out at the point of
    # saving rather than from a button with no icon.
    from user_service.identity.saml import is_safe_local_path

    icon = str(cfg.get("icon_url") or "").strip()
    if icon and not (icon.startswith("data:image/") or is_safe_local_path(icon)):
        raise HTTPException(
            400,
            "icon_url must be a path served by this deployment (a single leading '/', "
            "no backslash or whitespace) or a data:image/ URI. A remote URL would tell "
            "that host who is about to sign in.",
        )
    return cfg


def _provider_admin_dict(p: SsoProviderModel) -> dict:
    """The management view of a provider row.

    `client_secret_enc` is never returned in any shape — not even decrypted for
    an admin. `has_secret` is all the screen needs to distinguish "configured"
    from "never set", and it is what lets an edit form leave the field blank to
    mean "keep the existing secret".
    """
    out = {
        "id":         str(p.id),
        "provider":   p.provider,
        "client_id":  p.client_id,
        "enabled":    p.enabled,
        "tenant_id":  p.tenant_id,
        "config":     dict(p.config or {}),
        "has_secret": bool(p.client_secret_enc),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
    if p.provider == "saml":
        # Certificate expiry is computed here rather than in the browser: it
        # means parsing DER, and the screen that warns about an expiring
        # certificate must not be the thing that breaks when one is unparseable.
        from user_service.identity.saml_keys import key_summary
        from user_service.identity.saml_metadata import cert_health

        cfg = dict(p.config or {})
        out["saml_health"] = {
            "certs": cert_health([str(c) for c in (cfg.get("idp_x509_certs") or [])]),
            "metadata_sync": cfg.get("metadata_sync"),
            # Summary only — never `sp_key_enc`, which is why the sealed key is a
            # column and not a `config` entry.
            "sp_key": key_summary(p),
        }
    return out


@router.get("/sso/providers/manage")
async def manage_sso_providers(
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(hxguard.guard("identity.sso.read")),
):
    """Admin listing: every row, enabled or not, with its provisioning config.

    Separate from the anonymous `GET /sso/providers`, which a login page calls
    and which must not disclose `tenant_id`, `config` or disabled rows.
    """
    # Every tenant's rows until 2026-09-29 (8d).
    from velaris_shared.tenancy.identity import scope_to_caller
    rows = (await session.execute(await scope_to_caller(
        select(SsoProviderModel).order_by(SsoProviderModel.provider),
        SsoProviderModel, session, user))).scalars().all()
    return {"providers": [_provider_admin_dict(r) for r in rows]}


@router.post("/sso/providers", status_code=201)
async def create_sso_provider(
    body:    SsoProviderIn,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(hxguard.guard("identity.sso.configure")),
):
    # A provider the callback cannot service must not be saved: otherwise an
    # admin sees a button appear on the login page and only discovers it is not
    # wired when a user clicks it. `google`/`github` are built in; anything else
    # is accepted once it brings its own (SSRF-checked) endpoints.
    await _validated_custom_provider(body.provider, dict(body.config or {}))
    # The provider's tenant is the tenant its sign-ins JOIN. Stored as sent
    # until 2026-09-30: a tenant admin could register their own IdP for another
    # tenant and sign accounts into it.
    try:
        body.tenant_id = await tenant_for_write(session, user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    p = SsoProviderModel(
        tenant_id=body.tenant_id,
        provider=body.provider,
        client_id=body.client_id,
        client_secret_enc=await encrypt_credentials_vaulted(
            session, {"secret": body.client_secret}, tenant_id=body.tenant_id),
        config=_validated_provider_config(body.config),
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return _provider_admin_dict(p)


class SsoProviderPatch(BaseModel):
    client_id:     Optional[str]  = None
    client_secret: Optional[str]  = None   # omitted/blank ⇒ keep the stored one
    enabled:       Optional[bool] = None
    tenant_id:     Optional[str]  = None
    config:        Optional[dict] = None


@router.patch("/sso/providers/{provider_id}")
async def update_sso_provider(
    provider_id: uuid.UUID,
    body:        SsoProviderPatch,
    session:     AsyncSession = Depends(get_session),
    user:        AuthenticatedUser = Depends(hxguard.guard("identity.sso.configure")),
):
    """Edit a provider in place — chiefly its provisioning `config`.

    `provider` itself is not patchable: changing it would silently re-point an
    existing `client_id`/secret pair at a different identity provider. Delete
    and re-create instead.
    """
    p = await session.get(SsoProviderModel, provider_id)
    if not p:
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SSO provider not found")

    if body.client_id is not None:
        p.client_id = body.client_id
    if body.client_secret:            # blank means "unchanged", not "erase"
        p.client_secret_enc = await encrypt_credentials_vaulted(
            session, {"secret": body.client_secret}, tenant_id=p.tenant_id)
    if body.enabled is not None:
        p.enabled = body.enabled
    if body.tenant_id is not None:
        try:
            p.tenant_id = await tenant_for_write(session, user, body.tenant_id or None)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
    if body.config is not None:
        cfg = _validated_provider_config(body.config)
        # Re-run the endpoint contract: an edit can remove the very URLs that
        # made a custom provider usable, or repoint them somewhere internal.
        await _validated_custom_provider(p.provider, cfg)
        p.config = cfg

    await session.commit()
    await session.refresh(p)

    from velaris_shared.db.security_events import log_security_event
    await log_security_event(
        session, event_type="sso_provider_updated", severity="medium",
        user_id=user.user_id, resource_type="sso_provider", resource_id=str(p.id),
        action="update", outcome="success",
        # The allowlist key differs by protocol — SAML reads
        # `email_domain_allowlist`. Logging only the OIDC one recorded an empty
        # allowlist for every SAML edit, which is the opposite of what the audit
        # trail is for: "who widened auto-provisioning, and to what".
        details={"provider": p.provider, "enabled": p.enabled,
                 "allowed_domains": list(
                     (p.config or {}).get(
                         "email_domain_allowlist" if p.provider == "saml" else "allowed_domains",
                     ) or []),
                 "auto_provision": (p.config or {}).get("auto_provision", True),
                 "link_existing_by_email": bool((p.config or {}).get("link_existing_by_email", False)),
                 "secret_rotated": bool(body.client_secret)},
    )
    await session.commit()
    return _provider_admin_dict(p)


@router.delete("/sso/providers/{provider_id}", status_code=204)
async def delete_sso_provider(
    provider_id: uuid.UUID,
    session:     AsyncSession = Depends(get_session),
    user:        AuthenticatedUser = Depends(hxguard.guard("identity.sso.configure")),
):
    p = await session.get(SsoProviderModel, provider_id)
    if not p:
        raise HTTPException(404, "Provider not found")
    await session.delete(p)
    await session.commit()


# ── OAuth2 redirect helpers (Google + GitHub) ─────────────────────
# Full PKCE flow: frontend redirects to provider, gets code, posts code here,
# we exchange for token, get user info, create/find Velaris user, issue JWT.

OAUTH_ENDPOINTS = {
    "google": {
        "auth_url":   "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url":  "https://oauth2.googleapis.com/token",
        "userinfo":   "https://www.googleapis.com/oauth2/v3/userinfo",
        "emails":     None,          # email + email_verified come back inline
        "scope":      "openid email profile",
    },
    "github": {
        "auth_url":   "https://github.com/login/oauth/authorize",
        "token_url":  "https://github.com/login/oauth/access_token",
        "userinfo":   "https://api.github.com/user",
        # /user returns the *public profile* email, which the account holder
        # types in freely and GitHub never verifies. The verified set lives
        # here, and this is the only address we will accept.
        "emails":     "https://api.github.com/user/emails",
        "scope":      "read:user user:email",
    },
}


# ── OAuth `state` — CSRF defence ──────────────────────────────────
# The state used to be minted and returned, and never looked at again: any
# site could post its own provider code to /sso/callback and silently land a
# victim's browser in the attacker's account (login CSRF). Two things fix it,
# and both are needed:
#
#   1. This signature, which proves the state came from us, names the provider
#      it was issued for, and expires. Stateless on purpose — a nonce table
#      would need a migration and buys little, since an OAuth `code` is itself
#      single-use at the provider, so a replayed state fails the exchange.
#   2. The browser binding, which lives in the client: the value is held in
#      sessionStorage and compared on return. That is what an attacker cannot
#      forge — they can obtain a *valid* state of their own, but not write it
#      into the victim's session storage.
_SSO_STATE_TTL = 600  # seconds; a human OAuth round trip is well under this


def _sso_state_secret() -> str:
    """The HMAC key. Refuses to operate unconfigured.

    An empty key still produces a valid-looking signature that anyone can
    reproduce, which would silently reduce the state check to decoration. This
    is the one place that must fail loudly instead.
    """
    secret = get_settings().auth_secret
    if not secret or not secret.strip():
        raise HTTPException(
            503, "SSO is unavailable: the server has no auth secret configured.",
        )
    return secret


def _sso_state_sign(body: str) -> str:
    return hmac.new(
        _sso_state_secret().encode(), body.encode(), hashlib.sha256
    ).hexdigest()[:32]


def _mint_sso_state(provider_id: str, redirect_uri: str) -> str:
    """`<nonce>.<provider-row-id>.<redirect-digest>.<expiry>.<hmac>`.

    The *row id* rather than the provider name: `auth-url` and `callback` each
    used to pick "the first enabled row with this name", so in a multi-tenant
    deployment the callback could complete against a different tenant's client
    id than the one the browser was actually sent to. Binding the row removes
    the ambiguity instead of trying to re-derive the same answer twice.
    """
    body = ".".join((
        secrets.token_urlsafe(16),
        str(provider_id),
        hashlib.sha256(redirect_uri.encode()).hexdigest()[:16],
        str(int(_time.time()) + _SSO_STATE_TTL),
    ))
    return f"{body}.{_sso_state_sign(body)}"


def _verify_sso_state(state: str, redirect_uri: str) -> str | None:
    """Return the provider row id the state was minted for, or None.

    Signature, expiry and redirect_uri must all match the mint.
    """
    parts = (state or "").split(".")
    if len(parts) != 5:
        return None
    body, sig = ".".join(parts[:4]), parts[4]
    if not hmac.compare_digest(sig, _sso_state_sign(body)):
        return None
    _, provider_id, redirect_digest, expiry = parts[:4]
    if redirect_digest != hashlib.sha256(redirect_uri.encode()).hexdigest()[:16]:
        return None
    try:
        if int(expiry) <= int(_time.time()):
            return None
    except ValueError:
        return None
    return provider_id


@router.get("/sso/{provider}/auth-url", dependencies=[Depends(_sso_rl)])
async def get_sso_auth_url(
    provider:    str,
    redirect_uri: str,
    tenant_id:   Optional[str] = None,
    session:     AsyncSession = Depends(get_session),
):
    """Return the provider's OAuth2 authorization URL for the frontend to redirect to."""
    if provider == "saml":
        # A SAML row is not an OAuth provider: it has no authorize URL and its
        # login is a browser redirect we build ourselves. Falling through would
        # answer "no authorization URL configured", which reads as a broken
        # setup rather than the wrong endpoint.
        raise HTTPException(
            400,
            "SAML sign-in does not use this endpoint. Send the browser to "
            "/api/v1/auth/saml/{tenant}/login instead.",
        )
    rows = (await session.execute(
        select(SsoProviderModel)
        .where(SsoProviderModel.provider == provider)
        .where(SsoProviderModel.enabled == True)  # noqa: E712
        .order_by(SsoProviderModel.created_at)
    )).scalars().all()
    if tenant_id:
        from velaris_shared.tenancy.identity import same_tenant
        rows = [r for r in rows if await same_tenant(session, r.tenant_id, tenant_id)]
    p_row = rows[0] if rows else None
    if not p_row:
        raise HTTPException(404, f"SSO provider '{provider}' is not configured.")

    ep = _endpoints_for(p_row)
    if not ep.get("auth_url"):
        raise coded(
            404, AUTH_SSO_UNCONFIGURED_404,
            f"SSO provider '{provider}' has no authorization URL configured.",
        )
    await _assert_endpoints_safe(ep)
    state = _mint_sso_state(p_row.id, redirect_uri)
    # urlencode, not f-string concatenation: redirect_uri arrives decoded, so a
    # raw `&` in it used to inject extra parameters into the authorize request.
    query = urlencode({
        "client_id":     p_row.client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         ep["scope"],
        "state":         state,
    })
    return {"auth_url": f"{ep['auth_url']}?{query}", "state": state}


class SsoCallbackRequest(BaseModel):
    provider:     str
    code:         str
    redirect_uri: str
    state:        str = Field(..., max_length=256)
    device_id:    Optional[str] = Field(None, max_length=36)  # Group J


async def _provider_email(client, ep: dict, info: dict, access_token: str) -> tuple[str, bool]:
    """The address the provider vouches for, and whether it vouches at all.

    Only a *verified* address may name a Velaris account: the email decides
    which existing user an SSO identity binds to, and GitHub's profile email is
    free text the account holder sets. Unverified, it is an account-takeover
    primitive — claim `admin@company.com` on a throwaway GitHub account and the
    enrolment branch below hands over the real admin's session.

    A custom provider takes the OIDC path (the `email_verified` claim) unless it
    declares a GitHub-shaped `emails` endpoint of its own.
    """
    emails_url = ep.get("emails")
    if emails_url:
        resp = await client.get(emails_url, headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        })
        if resp.is_success:
            rows = [r for r in resp.json() if isinstance(r, dict)]
            verified = [r for r in rows if r.get("verified") and r.get("email")]
            primary  = next((r for r in verified if r.get("primary")), None)
            chosen   = primary or (verified[0] if verified else None)
            if chosen:
                return str(chosen["email"]), True
        return str(info.get("email") or ""), False

    # Google and any other OIDC-shaped provider: the claim is authoritative.
    claim    = info.get("email_verified")
    verified = claim is True or str(claim).strip().lower() == "true"
    return str(info.get("email") or ""), verified


async def _unique_username(session: AsyncSession, base: str) -> str:
    """A free username derived from the email local part.

    `admin@gmail.com` used to derive `admin` and hit the unique index, which
    surfaced as a 500 on an otherwise valid login.
    """
    candidate = "".join(c for c in base if c.isalnum() or c in "._-")[:60] or "user"
    for suffix in ("", *(f"-{n}" for n in range(2, 10))):
        name = f"{candidate}{suffix}"
        taken = (await session.execute(
            select(HelixUserModel.id).where(HelixUserModel.username == name).limit(1)
        )).scalar_one_or_none()
        if not taken:
            return name
    return f"{candidate}-{secrets.token_hex(4)}"


@router.post("/sso/callback", dependencies=[Depends(_sso_rl)])
async def sso_callback(
    request: Request,
    body:    SsoCallbackRequest,
    session: AsyncSession = Depends(get_session),
):
    """Exchange OAuth2 code for a Velaris JWT."""
    import httpx

    # Before anything else, including the outbound call: an unsigned callback
    # is a forgery attempt and must not even reach the provider.
    provider_id = _verify_sso_state(body.state, body.redirect_uri)
    if not provider_id:
        raise coded(
            400, AUTH_SSO_STATE_400,
            "Sign-in could not be verified. Start again from the sign-in page.",
        )

    try:
        p_row = await session.get(SsoProviderModel, uuid.UUID(provider_id))
    except ValueError:
        p_row = None
    # The state names the exact row the browser was sent to. It must still
    # exist, still be enabled, and still be the provider the client claims —
    # otherwise a state minted before an admin disabled or repointed a provider
    # would keep working.
    if not p_row or not p_row.enabled or p_row.provider != body.provider:
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SSO provider not configured")

    ep = _endpoints_for(p_row)
    if not ep.get("token_url") or not ep.get("userinfo"):
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SSO provider not configured")
    await _assert_endpoints_safe(ep)
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

        sso_email, email_verified = await _provider_email(
            client, ep, info, access_token,
        )

    sso_email    = sso_email.strip().lower()
    sso_subject  = str(info.get("sub") or info.get("id") or "")
    display_name = info.get("name") or info.get("login") or sso_email.split("@")[0]

    if not sso_email:
        raise HTTPException(400, "Provider did not return an email address")
    if not sso_subject:
        # An empty subject is not a harmless blank: it is the key the lookup
        # below matches on, so two subject-less identities would resolve to
        # each other's account.
        raise HTTPException(400, "Provider did not return a subject identifier")

    from velaris_shared.db.security_events import log_security_event
    client_ip = request.client.host if request.client else None

    # Provisioning policy lives on the provider row's JSON config, so enabling
    # it is an admin decision per provider rather than a platform default.
    # `allowed_domains` empty or absent means NO auto-provisioning: before this,
    # anyone on the public internet who completed the Google flow got an account.
    cfg             = dict(p_row.config or {})
    allowed_domains = {
        str(d).strip().lower().lstrip("@")
        for d in (cfg.get("allowed_domains") or []) if str(d).strip()
    }
    # `superadmin` is refused when the config is written; filtered again here so
    # a row edited directly in the database cannot mint one either.
    default_roles = [
        str(r) for r in (cfg.get("default_roles") or [])
        if str(r).strip() and str(r).strip().lower() != "superadmin"
    ] or ["viewer"]

    # Find or create Velaris user
    u = (await session.execute(
        select(HelixUserModel).where(
            (HelixUserModel.sso_provider == body.provider) &
            (HelixUserModel.sso_subject  == sso_subject)
        )
    )).scalar_one_or_none()

    if not u:
        # Email decides identity from here down, so it must be one the provider
        # vouches for — on both the enrolment and the provisioning branch.
        if not email_verified:
            await log_security_event(
                session, event_type="sso_email_unverified", severity="high",
                action="sso_login", outcome="denied", ip_address=client_ip,
                details={"provider": body.provider, "email": sso_email},
            )
            await session.commit()
            raise coded(
                403, AUTH_SSO_UNVERIFIED_403,
                f"{body.provider} has not verified this email address. "
                "Verify it with your identity provider, then sign in again.",
            )

        # Try to match by email (SSO enrolment). Case-insensitive: addresses are
        # not case-sensitive in practice, and an exact match would fork one
        # person into two accounts on a capitalisation difference.
        u = (await session.execute(
            select(HelixUserModel).where(func.lower(HelixUserModel.email) == sso_email)
        )).scalar_one_or_none()
        if u:
            # **Enrolment by address is a claim from outside, not proof.**
            # `email_verified` says the provider believes the person controls
            # that mailbox; it says nothing about whether the *Velaris account*
            # holding that address is theirs. Whoever administers the directory
            # chooses those addresses, so adopting on a match hands them
            # whichever local account already uses it, with its roles. The SAML
            # ACS refuses this for the same reason; an administrator links the
            # account deliberately instead
            # (`POST /identity/provisioning/links`).
            refusal = None
            if not bool(cfg.get("link_existing_by_email", False)):
                refusal = "email_taken"
            elif u.password_hash:
                # Even where the deployment has switched linking back on: a row
                # with a password is somebody who signs in here directly, and an
                # IdP does not get to become them.
                refusal = "local_account_protected"
            if refusal:
                await log_security_event(
                    session, event_type="sso_provision_refused", severity="high",
                    action="sso_login", outcome="denied", ip_address=client_ip,
                    details={"provider": body.provider, "reason": refusal,
                             "email": sso_email, "username": u.username},
                )
                await session.commit()
                raise coded(
                    403, AUTH_SSO_PROVISION_403,
                    "An account already uses this email address and is not linked "
                    "to your identity provider. Ask an administrator to link it.",
                )
            u.sso_provider = body.provider
            u.sso_subject  = sso_subject
        else:
            domain = sso_email.rsplit("@", 1)[-1]
            if domain not in allowed_domains:
                await log_security_event(
                    session, event_type="sso_provision_refused", severity="medium",
                    action="sso_login", outcome="denied", ip_address=client_ip,
                    details={"provider": body.provider, "domain": domain,
                             "allowlist_configured": bool(allowed_domains)},
                )
                await session.commit()
                raise coded(
                    403, AUTH_SSO_PROVISION_403,
                    "This account is not provisioned in Velaris. "
                    "Ask an administrator to create it, or to allow your email domain.",
                )
            u = HelixUserModel(
                username=await _unique_username(session, sso_email.split("@")[0]),
                email=sso_email,
                display_name=display_name,
                roles=list(default_roles),
                is_active=True,
                sso_provider=body.provider,
                sso_subject=sso_subject,
            )
            session.add(u)
            await log_security_event(
                session, event_type="sso_user_provisioned", severity="medium",
                action="sso_login", outcome="success", ip_address=client_ip,
                details={"provider": body.provider, "email": sso_email,
                         "roles": list(default_roles)},
            )

    # The password path has always refused disabled accounts; this one never
    # did, so disabling a user left their SSO button working. A deleted account
    # (mig 223) is refused too — the provider cannot bring it back.
    if not u.is_active or u.deleted_at is not None:
        raise coded(403, AUTH_DISABLED_403, "Account is disabled. Contact an administrator.")

    u.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(u)

    # Group J: SSO sessions are device-bound too
    from velaris_shared.auth.devices import get_or_create_device
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
    _: AuthenticatedUser = Depends(hxguard.guard("identity.tokens.read")),
):
    return {"token_expiry_days": await _get_token_expiry_days(session)}


class TokenExpiryRequest(BaseModel):
    token_expiry_days: int = Field(..., ge=1, le=3650)

@router.put("/settings/token-expiry")
async def set_token_expiry(
    body: TokenExpiryRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.tokens.configure")),
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
    if not await _check_refresh_rate(client_ip):
        raise coded(429, AUTH_RATE_LIMIT_429, "Too many refresh attempts. Please wait before retrying.")

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
    from velaris_shared.auth.devices import check_device_on_refresh
    device_id = uuid.UUID(str(row[1])) if row[1] else None
    if not await check_device_on_refresh(
        session, device_id,
        request.headers.get("user-agent", ""),
        client_ip,
    ):
        await session.commit()  # persist the chain revocation
        raise coded(401, AUTH_DEVICE_MISMATCH_401, "Session device check failed. Please log in again.")

    user = await session.get(HelixUserModel, uuid.UUID(row[0]))
    if not user or not user.is_active or user.deleted_at is not None:
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
            # Every other process reloads its revoked set within seconds.
            from velaris_shared import generations
            await generations.bump(session, "revocation", reason="logout")

    await session.commit()
    if credentials:
        # And this one refuses the token from the next request on.
        from velaris_shared.auth.dependencies import note_revoked
        note_revoked(raw_hash)
    return {"ok": True}


# ── Group J: device sessions ──────────────────────────────────────

@router.get("/devices")
async def list_devices(
    user:    AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """The current user's active devices (sign-in sessions)."""
    from velaris_shared.db.models import AuthDeviceModel

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
    from velaris_shared.db.models import AuthDeviceModel
    from velaris_shared.auth.devices import revoke_device

    dev = await session.get(AuthDeviceModel, device_id)
    # 404 for both missing and foreign devices — don't confirm existence
    if dev is None or (dev.user_id != user.user_id and "admin" not in (user.roles or [])):
        raise HTTPException(404, "Device not found")
    if dev.revoked_at is not None:
        return {"ok": True, "revoked_tokens": 0}

    count = await revoke_device(session, dev, revoked_by=user.user_id)
    try:
        from velaris_shared.db.security_events import log_security_event
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
    from velaris_shared.auth import webauthn_service

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
    from velaris_shared.auth import webauthn_service

    try:
        cred = await webauthn_service.complete_registration(
            session, user.user_id, body.credential, body.device_name or "Passkey",
        )
    except Exception as e:
        raise HTTPException(400, f"Passkey registration failed: {e}")

    try:
        from velaris_shared.db.security_events import log_security_event
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
    from velaris_shared.db.models import WebAuthnCredentialModel

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
    from velaris_shared.db.models import WebAuthnCredentialModel

    cred = await session.get(WebAuthnCredentialModel, credential_id)
    # 404 for both missing and foreign credentials — don't confirm existence
    if cred is None or cred.user_id != user.user_id:
        raise HTTPException(404, "Passkey not found")
    cred.revoked_at = datetime.now(timezone.utc)
    try:
        from velaris_shared.db.security_events import log_security_event
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
    from velaris_shared.auth import webauthn_service

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
    from velaris_shared.auth import webauthn_service
    from velaris_shared.auth.devices import get_or_create_device

    try:
        cred, user_verified = await webauthn_service.complete_authentication(
            session, body.credential, "login",
        )
    except Exception as e:
        try:
            from velaris_shared.db.security_events import log_security_event
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
    if not user or not user.is_active or user.deleted_at is not None:
        raise coded(403, AUTH_DISABLED_403, "Account is disabled. Contact an administrator.")
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
        from velaris_shared.db.security_events import log_security_event
        from velaris_shared.hxstream.emitter import emit_trace
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
    from velaris_shared.auth import webauthn_service

    try:
        options_json = await webauthn_service.begin_authentication(
            session, user.user_id, purpose="stepup",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await session.commit()
    return Response(content=options_json, media_type="application/json",
                    headers={"Cache-Control": "no-store"})
