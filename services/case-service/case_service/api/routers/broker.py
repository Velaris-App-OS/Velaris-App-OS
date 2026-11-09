"""Marketplace Layer-2 — the scoped data broker (`/api/v1/broker`).

The ONLY data path for app containers. No ambient database credentials ever
enter a container; instead the container holds an opaque broker token whose
hash lives on its capability grant. Every call:

  1. authenticates by token hash against a GRANTED container grant
     (grant status is checked per call — revocation is instant),
  2. is tenant-pinned by construction (the grant's tenant, never a parameter),
  3. is scope-checked (`cases.read` or `cases.read:<case_type>`),
  4. is rate-limited per grant,
  5. is logged to marketplace_network_log as `broker://…` — the same ledger
     as egress traffic.

Read-only by design in this release. Writes are a future, separately-scoped
capability. 404 anti-oracle on every failure mode of the token.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    MarketplaceCapabilityGrantModel,
    MarketplaceInstallModel,
    TenantModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.llm.guard import _RateLimiter
from velaris_shared.apps import grant_checks as mkt_grants

router = APIRouter(prefix="/broker", tags=["broker"])

_rate_limiter = _RateLimiter(max_calls=60, window_seconds=60)


async def get_broker_grant(
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
) -> MarketplaceCapabilityGrantModel:
    """Token > hash > GRANTED container grant. Uniform 404 on every failure
    mode — a probing container learns nothing."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(404, "Not found")
    raw = authorization[7:]
    if not raw:
        raise HTTPException(404, "Not found")
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    rows = (await session.execute(
        select(MarketplaceCapabilityGrantModel).where(
            MarketplaceCapabilityGrantModel.status == "granted",
            MarketplaceCapabilityGrantModel.descriptor_format ==
            mkt_grants.CONTAINER_DESCRIPTOR_FORMAT,
        )
    )).scalars().all()
    # Constant-time compare (defense in depth — both sides are already SHA-256
    # digests). A grant with no stored hash can never match.
    grant = None
    for g in rows:
        stored = (g.granted or {}).get("broker_token_hash")
        if stored and secrets.compare_digest(str(stored), token_hash):
            grant = g
            break
    if grant is None:
        raise HTTPException(404, "Not found")
    allowed, retry_after = _rate_limiter.is_allowed(str(grant.id))
    if not allowed:
        raise HTTPException(429, "Broker rate limit exceeded",
                            headers={"Retry-After": str(retry_after)})
    return grant


def _scoped_case_types(grant: MarketplaceCapabilityGrantModel) -> set[str] | None:
    """None = every case type of the tenant; a set = only those (id or name).
    No cases.* scope at all = nothing."""
    scopes = (grant.granted or {}).get("scopes", [])
    if "cases.read" in scopes:
        return None
    qualified = {s.split(":", 1)[1] for s in scopes
                 if s.startswith("cases.read:") and s.split(":", 1)[1]}
    return qualified


async def _tenant_uuid(session: AsyncSession, grant) -> uuid.UUID | None:
    """Grants carry the tenant SLUG; cases/case-types key the tenant UUID."""
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == (grant.tenant_id or "default"))
    )).scalars().first()
    return tenant.id if tenant else None


def _tenant_matches(row_tenant: uuid.UUID | None, tenant_uuid: uuid.UUID | None,
                    grant_slug: str) -> bool:
    """NULL tenant rows belong to 'default' — the platform-wide back-compat
    convention (single-tenant installs never stamped tenant ids)."""
    if row_tenant is not None:
        return tenant_uuid is not None and row_tenant == tenant_uuid
    return (grant_slug or "default") == "default"


async def _log(grant, path: str, status: str) -> None:
    await mkt_grants.log_marketplace_call(
        grant_id=str(grant.id), package_id=grant.package_id,
        url=f"broker://{path}", method="GET", status=status,
        is_declared=(status == "allowed"))


async def _manifest_allows_cases(session: AsyncSession, grant) -> bool:
    """The package's OWN declaration, as a ceiling over the operator's grant.

    Two independent questions, and until #29 was enforced only one was asked:

      * the capability grant — what the operator agreed to expose,
      * the manifest — what the package said it needed.

    A call needs both. The grant can offer `cases.read` and this still refuses,
    because a package that never declared `case` has no business reading one
    however generous the grant is. It only narrows; it can never widen.

    An install from before enforcement has no stored declaration, and NULL is
    not "declared nothing" — it is "not recorded". Those keep working on the
    grant alone rather than losing access to an upgrade nobody asked for.
    """
    from velaris_shared.apps.manifest_version import declares_access

    if grant.install_id is None:
        return True
    install = await session.get(MarketplaceInstallModel, grant.install_id)
    if install is None:
        return True
    return declares_access(install.declared_permissions, "case")


def _type_allowed(allowed: set[str] | None, ct: CaseTypeModel | None) -> bool:
    if allowed is None:
        return True
    if ct is None:
        return False
    return str(ct.id) in allowed or (ct.name or "") in allowed


def _case_view(c: CaseInstanceModel) -> dict:
    return {
        "id":               str(c.id),
        "case_type_id":     str(c.case_type_id),
        "status":           c.status,
        "priority":         c.priority,
        "current_stage_id": c.current_stage_id,
        "data":             c.data or {},
        "created_at":       c.created_at.isoformat() if c.created_at else None,
        "updated_at":       c.updated_at.isoformat() if getattr(c, "updated_at", None) else None,
    }


@router.get("/cases")
async def broker_list_cases(
    case_type: str | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    grant: MarketplaceCapabilityGrantModel = Depends(get_broker_grant),
):
    if not await _manifest_allows_cases(session, grant):
        await _log(grant, "cases", "blocked")
        raise HTTPException(403, "This package's manifest declares no access to cases")

    allowed = _scoped_case_types(grant)
    if allowed is not None and not allowed:
        await _log(grant, "cases", "blocked")
        raise HTTPException(403, "This grant has no cases.read scope")

    tenant_uuid = await _tenant_uuid(session, grant)
    type_rows = (await session.execute(select(CaseTypeModel))).scalars().all()
    visible_ids = [
        ct.id for ct in type_rows
        if _tenant_matches(ct.tenant_id, tenant_uuid, grant.tenant_id)
        and _type_allowed(allowed, ct)
    ]
    if case_type:
        match = next((ct for ct in type_rows
                      if str(ct.id) == case_type or ct.name == case_type), None)
        if match is None or match.id not in visible_ids:
            await _log(grant, f"cases?case_type={case_type}", "blocked")
            raise HTTPException(403, "Case type is outside this grant's scopes")
        visible_ids = [match.id]
    if not visible_ids:
        await _log(grant, "cases", "allowed")
        return {"cases": []}

    tenant_clause = (CaseInstanceModel.tenant_id == tenant_uuid)
    if (grant.tenant_id or "default") == "default":
        tenant_clause = tenant_clause | CaseInstanceModel.tenant_id.is_(None)
    rows = (await session.execute(
        select(CaseInstanceModel)
        .where(tenant_clause, CaseInstanceModel.case_type_id.in_(visible_ids),
               # 199 — a marketplace package must not read a deleted case.
               # This is the surface a THIRD PARTY's code sees, so it is the
               # last place a hidden case should still be legible.
               CaseInstanceModel.is_deleted.is_(False))
        .order_by(CaseInstanceModel.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )).scalars().all()
    await _log(grant, "cases", "allowed")
    return {"cases": [_case_view(c) for c in rows]}


@router.get("/cases/{case_id}")
async def broker_get_case(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    grant: MarketplaceCapabilityGrantModel = Depends(get_broker_grant),
):
    # Checked before the id is even parsed: a package outside its declaration
    # must not be able to tell a malformed id from a real one.
    if not await _manifest_allows_cases(session, grant):
        await _log(grant, f"cases/{case_id}", "blocked")
        raise HTTPException(404, "Case not found")
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(404, "Case not found")
    case = await session.get(CaseInstanceModel, cid)
    # Tenant pinning and scope check collapse into the same 404 — the broker
    # never confirms the existence of anything outside the grant.
    tenant_uuid = await _tenant_uuid(session, grant)
    if case is None or not _tenant_matches(case.tenant_id, tenant_uuid, grant.tenant_id):
        await _log(grant, f"cases/{case_id}", "blocked")
        raise HTTPException(404, "Case not found")
    ct = await session.get(CaseTypeModel, case.case_type_id)
    if not _type_allowed(_scoped_case_types(grant), ct):
        await _log(grant, f"cases/{case_id}", "blocked")
        raise HTTPException(404, "Case not found")
    await _log(grant, f"cases/{case_id}", "allowed")
    return _case_view(case)
