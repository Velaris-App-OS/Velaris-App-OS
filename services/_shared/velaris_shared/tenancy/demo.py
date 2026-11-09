"""Demo tenants — one per website sign-up (2026-11-04, Nov_TODO §0 "SECURITY").

Until this, every website sign-up was made `admin` of the install's DEFAULT
tenant, which `is_platform_operator` reads as the install's operator: every
registrant could list every tenant's users, change default-tenant accounts and
create or delete tenants. Now each sign-up gets a tenant of its own and is
`admin` there only.

A demo tenant is marked `settings.demo = {request_key, kind, email, created_at,
expires_at}`. Only a tenant carrying that mark is ever expired or deleted here.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

SLUG_PREFIX = "demo-"
GROUP_NAME = "Demo Users"
_STAFF_PORTAL = uuid.UUID("00000000-0000-0000-0000-000000000001")   # migration 033
_ADMIN_ROLE = "00000000-0000-0001-0000-000000000001"                 # access role `admin`


class DemoError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def slug_for(request_key: str) -> str:
    """Same key, same tenant: a retried call finds what the first one made."""
    return SLUG_PREFIX + hashlib.sha256(request_key.encode()).hexdigest()[:16]


def is_demo(tenant: Any) -> bool:
    return isinstance(getattr(tenant, "settings", None), dict) and isinstance(
        tenant.settings.get("demo"), dict)


def _parse(ts: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def provision(session: AsyncSession, *, request_key: str, username: str, email: str,
                    password: str, display_name: str | None, kind: str) -> dict:
    """Create (or find again) the tenant, its `admin` account, the account's
    directory row (what puts the token in the tenant) and a "Demo Users" access
    group holding the built-in `admin` access role. In the caller's transaction."""
    from velaris_shared.auth.passwords import hash_password_async
    from velaris_shared.config import get_settings
    from velaris_shared.db.models import HelixUserModel, TenantModel, UserDirectoryModel

    cfg = get_settings()
    slug = slug_for(request_key)
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug))).scalars().first()

    existing = (await session.execute(select(HelixUserModel).where(or_(
        HelixUserModel.username == username, HelixUserModel.email == email)))).scalars().first()
    if existing is not None:
        row = (await session.execute(select(UserDirectoryModel).where(
            UserDirectoryModel.user_id == str(existing.id)))).scalars().first()
        if tenant is None or row is None or row.tenant_id not in (slug, str(tenant.id)):
            # Somebody else's account — the default tenant's above all. Never
            # touched from here.
            raise DemoError(409, "Username or email already in use.")
        # A retry of OUR provision: the password the caller will email is the
        # one that works.
        existing.password_hash = await hash_password_async(password)
        existing.password_change_required = True
        return _result(tenant, existing)

    if tenant is None:
        active = (await session.execute(select(func.count()).select_from(TenantModel).where(
            TenantModel.slug.like(SLUG_PREFIX + "%"), TenantModel.status == "active"))).scalar_one()
        if active >= cfg.demo_max_tenants:
            raise DemoError(503, "The demo is full right now; the sign-up will be retried.")
        tenant = await _new_tenant(session, slug, request_key, kind, email,
                                   display_name or username)

    user = HelixUserModel(
        username=username, email=email, display_name=display_name,
        password_hash=await hash_password_async(password), roles=["admin"],
        password_change_required=True)
    session.add(user)
    await session.flush()
    session.add(UserDirectoryModel(
        id=uuid.uuid4(), user_id=str(user.id), email=email,
        display_name=display_name or username, tenant_id=slug, is_active=True,
        metadata_json={"created_by": "demo-signup"}))

    await _join_group(session, slug, username)
    log.info("demo tenant %s provisioned for %s", slug, username)
    return _result(tenant, user)


async def _new_tenant(session: AsyncSession, slug: str, request_key: str, kind: str,
                      email: str, display: str):
    from velaris_shared.config import get_settings
    from velaris_shared.db.models import TenantModel

    cfg = get_settings()
    now = _now()
    tenant = TenantModel(
        id=uuid.uuid4(), slug=slug, name=f"Demo — {display}"[:255],
        description="Website demo tenant; deleted when it expires.", status="active",
        settings={"demo": {
            "request_key": request_key, "kind": kind, "email": email,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=cfg.demo_tenant_days)).isoformat()}},
        max_users=cfg.demo_max_users, max_cases=cfg.demo_max_cases)
    session.add(tenant)
    await session.flush()
    return tenant


async def _join_group(session: AsyncSession, slug: str, username: str) -> None:
    from velaris_shared.db.models import AccessGroupModel, OperatorAccessGroupModel

    group = (await session.execute(select(AccessGroupModel).where(
        AccessGroupModel.tenant_id == slug, AccessGroupModel.name == GROUP_NAME))).scalars().first()
    if group is None:
        group = AccessGroupModel(
            id=uuid.uuid4(), name=GROUP_NAME, tenant_id=slug, portal_id=_STAFF_PORTAL,
            description="The demo tenant's own accounts (full access, this tenant only)",
            role_ids=[_ADMIN_ROLE])
        session.add(group)
        await session.flush()
    if (await session.execute(select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == username,
            OperatorAccessGroupModel.access_group_id == group.id))).scalars().first() is None:
        session.add(OperatorAccessGroupModel(
            id=uuid.uuid4(), operator_id=username, access_group_id=group.id, is_primary=True,
            assigned_by="demo-signup"))
    await session.flush()


async def move_existing(session: AsyncSession, *, request_key: str, username: str,
                        kind: str) -> dict:
    """Move an account the website made BEFORE 2026-11-04 (an `admin` of the
    default tenant) into a demo tenant of its own. The registration service
    names it — it alone knows which accounts are website sign-ups. Its access
    groups in the default tenant are left; the cases it made there stay there.
    Refused for a superadmin and for an account already in some other tenant.
    Idempotent: a second call finds the tenant the first made."""
    from velaris_shared.db.models import (
        AccessGroupModel, HelixUserModel, OperatorAccessGroupModel, TenantModel,
        UserDirectoryModel,
    )
    from velaris_shared.tenancy.identity import DEFAULT_TENANT, resolve_tenant

    user = (await session.execute(select(HelixUserModel).where(
        HelixUserModel.username == username))).scalars().first()
    if user is None or user.deleted_at is not None:
        raise DemoError(404, "No such account.")
    if user.is_superadmin or "superadmin" in (user.roles or []):
        raise DemoError(409, "A superadmin is never moved.")
    slug = slug_for(request_key)
    row = (await session.execute(select(UserDirectoryModel).where(
        UserDirectoryModel.user_id == str(user.id)))).scalars().first()
    default = await resolve_tenant(session, DEFAULT_TENANT)
    default_refs = {DEFAULT_TENANT} | ({str(default[0])} if default else set())
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug))).scalars().first()
    if row is not None and row.tenant_id and row.tenant_id not in default_refs:
        if tenant is not None and row.tenant_id in (slug, str(tenant.id)):
            return {**_result(tenant, user), "moved": False}
        raise DemoError(409, "The account belongs to another tenant; it is not moved.")
    if tenant is None:
        tenant = await _new_tenant(session, slug, request_key, kind, user.email,
                                   user.display_name or username)
    if row is None:
        session.add(UserDirectoryModel(
            id=uuid.uuid4(), user_id=str(user.id), email=user.email,
            display_name=user.display_name or username, tenant_id=slug, is_active=True,
            metadata_json={"created_by": "demo-move"}))
    else:
        row.tenant_id = slug
    # Its memberships in the default tenant's groups go (they were the demo's).
    ids = (await session.execute(select(AccessGroupModel.id).where(
        AccessGroupModel.tenant_id.in_(default_refs)))).scalars().all()
    if ids:
        for m in (await session.execute(select(OperatorAccessGroupModel).where(
                OperatorAccessGroupModel.operator_id.in_(
                    (username, user.email, str(user.id))),
                OperatorAccessGroupModel.access_group_id.in_(ids)))).scalars().all():
            await session.delete(m)
    await _join_group(session, slug, username)
    # While it was the default tenant's admin it was a platform operator and
    # could mint lasting credentials there — refresh tokens, devices, MCP
    # grants, agent tokens. Every one ends now; the user signs in again into
    # the new tenant (its password is unchanged).
    from velaris_shared.identity.lifecycle import _end_sessions
    await _end_sessions(session, user, "demo-move", {}, None)
    from velaris_shared.tenancy import identity as _ident
    _ident.clear_cache()
    log.info("demo account %s moved to its own tenant %s", username, slug)
    return {**_result(tenant, user), "moved": True,
            "left_behind": await _made_as_operator(session, user)}


#: Tables where an operator's row has no tenant and so applies platform-wide.
_OPERATOR_ROWS = ("connector_registry", "scim_tokens", "audit_stream_targets",
                  "security_rules", "sovereignty_rules", "agents", "saved_reports")


async def _made_as_operator(session: AsyncSession, user: Any) -> dict[str, int]:
    """Rows the account created with no tenant while it was a platform
    operator. Reported for the operator to review, never removed here — some
    may be wanted. Absent tables are skipped."""
    from sqlalchemy import text
    out: dict[str, int] = {}
    who = {"a": str(user.id), "b": user.username, "c": user.email}
    for t in _OPERATOR_ROWS:
        try:
            async with session.begin_nested():
                n = (await session.execute(text(
                    f"SELECT COUNT(*) FROM {t} WHERE tenant_id IS NULL "
                    "AND created_by IN (:a, :b, :c)"), who)).scalar_one()
        except Exception:                                    # noqa: BLE001
            continue
        if n:
            out[t] = int(n)
    return out


def _result(tenant: Any, user: Any) -> dict:
    demo = tenant.settings.get("demo") or {}
    return {"tenant_id": str(tenant.id), "tenant_slug": tenant.slug,
            "user_id": str(user.id), "username": user.username,
            "expires_at": demo.get("expires_at")}


async def extend(session: AsyncSession, tenant_ref: str, days: int) -> dict:
    """The sales extension: `days` more from now or from the current expiry,
    whichever is later. Re-opens a demo tenant the expiry had archived."""
    from velaris_shared.db.models import TenantModel
    from velaris_shared.tenancy.identity import resolve_tenant

    hit = await resolve_tenant(session, tenant_ref)
    tenant = await session.get(TenantModel, hit[0]) if hit else None
    if tenant is None or not is_demo(tenant):
        raise DemoError(404, "Not a demo tenant.")
    demo = dict(tenant.settings["demo"])
    base = max(_parse(demo.get("expires_at")) or _now(), _now())
    demo["expires_at"] = (base + timedelta(days=days)).isoformat()
    demo.pop("archived_at", None)
    tenant.settings = {**tenant.settings, "demo": demo}
    if tenant.status == "archived":
        tenant.status = "active"
    await session.flush()
    return {"tenant_id": str(tenant.id), "expires_at": demo["expires_at"]}


async def expire_due(session: AsyncSession, held_count) -> dict[str, int]:
    """Archive demo tenants past `expires_at`; delete those archived
    `demo_delete_after_days` ago, accounts first. Only tenants carrying the
    demo mark. One commit per tenant. `held_count(session, tenant_uuid)` is the
    legal-hold count (case-service's): a held tenant is kept, as a manual
    delete is refused."""
    from velaris_shared.config import get_settings
    from velaris_shared.db.models import (
        AccessGroupModel, HelixUserModel, TenantModel, UserDirectoryModel,
    )
    from velaris_shared.identity.lifecycle import purge_user

    keep = timedelta(days=get_settings().demo_delete_after_days)
    now = _now()
    out = {"archived": 0, "deleted": 0, "blocked": 0}
    rows = (await session.execute(select(TenantModel).where(
        TenantModel.slug.like(SLUG_PREFIX + "%")))).scalars().all()
    for t in rows:
        if not is_demo(t):
            continue
        demo = dict(t.settings["demo"])
        exp = _parse(demo.get("expires_at"))
        if exp is None or exp > now:
            continue
        if t.status != "archived":
            demo["archived_at"] = now.isoformat()
            t.settings = {**t.settings, "demo": demo}
            t.status = "archived"
            await session.commit()
            out["archived"] += 1
            continue
        archived = _parse(demo.get("archived_at")) or exp
        if archived + keep > now:
            continue
        if await held_count(session, t.id):
            out["blocked"] += 1
            continue
        refs = (t.slug, str(t.id))
        ids = (await session.execute(select(UserDirectoryModel.user_id).where(
            UserDirectoryModel.tenant_id.in_(refs)))).scalars().all()
        for uid in ids:
            try:
                u = await session.get(HelixUserModel, uuid.UUID(str(uid)))
            except ValueError:
                u = None
            if u is not None:
                await purge_user(session, u, actor="demo-expiry")
        for row in (await session.execute(select(UserDirectoryModel).where(
                UserDirectoryModel.tenant_id.in_(refs)))).scalars().all():
            await session.delete(row)
        for g in (await session.execute(select(AccessGroupModel).where(
                AccessGroupModel.tenant_id.in_(refs)))).scalars().all():
            await session.delete(g)
        await session.delete(t)
        await session.commit()
        out["deleted"] += 1
    return out

