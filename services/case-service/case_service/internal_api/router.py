"""case-service's internal API routes — `/internal/v1/*` (R5e step 7).

Called by commerce-service, portal-service and user-service only, straight to case-service (never through the
gateway, which refuses `/internal`), with the install's internal service secret
(`velaris_shared.internal.auth`). There is no user: every route carries its
tenant explicitly, runs the same case-service code the caller used to run
in-process, and is idempotent so a retried call does no harm.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.session import get_session
from velaris_shared.internal.auth import require_internal_caller
from velaris_shared.internal.client import register_local

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal"],
                   dependencies=[Depends(require_internal_caller)],
                   include_in_schema=False)

def _local(path: str, model, fn) -> None:
    """Register `fn` for in-process callers (velaris_shared.internal.client local
    mode): same request model, same validation, the caller's own session."""
    async def handler(session, payload: dict):
        try:
            body = model(**payload)
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await fn(session, body)

    register_local(path, handler)


_KEY = Field(min_length=1, max_length=200)
_TENANT = Field(default=None, max_length=200)


# ── Checkout: the case behind an order ───────────────────────────────────────

class OrderCaseIn(BaseModel):
    kind: str = Field(pattern="^(order|return|complaint)$")
    tenant: str | None = _TENANT
    request_key: str = _KEY
    data: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(min_length=1, max_length=200)
    rollout_subject: list[str | None] = Field(default_factory=list, max_length=4)
    via: str = Field(default="checkout", max_length=50)
    parent_case_id: uuid.UUID | None = None


async def _open_order_case(session: AsyncSession, body: OrderCaseIn):
    from case_service.internal_api import order_cases

    return await order_cases.open_case(
        session, kind=body.kind, tenant=body.tenant, request_key=body.request_key,
        data=body.data, created_by=body.created_by,
        rollout_subject=tuple(body.rollout_subject), via=body.via,
        parent_case_id=body.parent_case_id)


# ── Payments: advance a case whose step a payment completed ───────────────────

class CaseIdIn(BaseModel):
    case_id: uuid.UUID


async def _auto_advance(session: AsyncSession, body: CaseIdIn):
    """Advance the case if its current stage is now complete. A no-op otherwise."""
    from case_service.api.routers.cases import _auto_advance_if_complete
    from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel

    case = await session.get(CaseInstanceModel, body.case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    if not case.case_type_id:
        return {"advanced": False, "stage_id": None}
    ct = await session.get(CaseTypeModel, case.case_type_id)
    if ct is None:
        return {"advanced": False, "stage_id": None}
    new_stage, held = await _auto_advance_if_complete(
        session, case.id, case.current_stage_id, ct.definition_json)
    return {"advanced": new_stage is not None, "stage_id": new_stage, "held_by_risk": held}


# ── Marketplace packs: bundle + rails ─────────────────────────────────────────

class BundleIn(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    bundle: dict[str, Any]


async def _apply_bundle(session: AsyncSession, body: BundleIn):
    """`hxdeploy.packager.apply_bundle` — upserts, so a repeat is harmless."""
    from case_service.hxdeploy.packager import apply_bundle as _apply

    return await _apply(session, body.bundle, body.tenant_id)


class RailsIn(BaseModel):
    rails: Any


async def _validate_rails(session: AsyncSession, body: RailsIn):
    from case_service.engage import rail_config

    try:
        return {"rails": rail_config.validate_many(body.rails)}
    except rail_config.RailError as exc:
        raise HTTPException(422, str(exc)) from exc


class RailsUpsertIn(BaseModel):
    tenant_uuid: uuid.UUID
    package_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=50)
    rails: list[dict[str, Any]] = Field(max_length=100)


async def _upsert_rails(session: AsyncSession, body: RailsUpsertIn):
    from case_service.engage import rail_config

    try:
        rails = rail_config.validate_many(body.rails)    # never trust the caller's copy
    except rail_config.RailError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await rail_config.upsert_from_pack(
        session, tenant_uuid=body.tenant_uuid, package_id=body.package_id,
        version=body.version, rails=rails)


# ── Marketplace grants: egress policy ────────────────────────────────────────

class EgressPolicyIn(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    package_id: str = Field(min_length=1, max_length=200)
    fallback_subject: str = Field(min_length=1, max_length=200)
    domains: list[str] = Field(max_length=200)


async def _egress_policy(session: AsyncSession, body: EgressPolicyIn):
    """Register the package's agent and ask HxArbiter, per declared host, whether
    it may be reached. The code `grants.approve` ran inline before the split."""
    from hxarbiter_core import SubjectKind

    from case_service.agents import attribution, identity
    from case_service.hxarbiter import pep

    # F-00: the subject is a registered agent keyed on the PACKAGE, with an empty
    # capability ceiling (see marketplace.grants for why).
    agent_id = await identity.register_out_of_band(
        tenant_id=body.tenant_id,
        slug=f"marketplace.{body.package_id}",
        kind="marketplace",
        display_name=f"Marketplace package {body.package_id}",
        package_id=body.package_id,
    ) or body.fallback_subject      # unresolvable registry: the old shape, not nothing
    allowed: list[str] = []
    for domain in body.domains:
        if await pep.check(
            session,
            tenant_id=body.tenant_id,
            action="egress.http",
            subject_id=agent_id,
            subject_kind=SubjectKind.AGENT,
            subject_attrs=attribution.subject_attrs("marketplace"),
            resource_kind="package",
            resource_id=body.package_id,
            context={"target_domain": str(domain).lower(), "purpose": "connector_egress"},
            legacy_allowed=True,   # the manifest already declared it
            claimed_by="marketplace.grants",
        ):
            allowed.append(domain)
    return {"agent_id": agent_id, "allowed_domains": allowed}


# ── Model market: HxSentinel ──────────────────────────────────────────────────

class TenantIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)


async def _labelled_features(session: AsyncSession, body: TenantIn):
    """The tenant's labelled history, as the risk model trains on it."""
    from case_service.sentinel import model as risk_model

    X, y = await risk_model._labelled_features(session, body.tenant)
    return {"X": X.tolist(), "y": y.tolist()}


class FeatureContextsIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    case_ids: list[uuid.UUID] = Field(max_length=500)


async def _feature_contexts(session: AsyncSession, body: FeatureContextsIn):
    """A pack's feature context for each case; a case that cannot be built is skipped."""
    from case_service.sentinel import packs as sentinel_packs
    from velaris_shared.db.models import CaseInstanceModel
    from velaris_shared.tenancy.identity import resolve_tenant

    resolved = await resolve_tenant(session, body.tenant)
    if resolved is None:
        raise HTTPException(404, "tenant not found")
    cases = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id.in_(body.case_ids),
            CaseInstanceModel.tenant_id == resolved[0]))).scalars().all()
    contexts = []
    for case in cases:
        try:
            contexts.append(await sentinel_packs.build_feature_context(session, case, body.tenant))
        except Exception:                                        # noqa: BLE001
            continue
    return {"contexts": contexts}


class SignalPackIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    manifest: dict[str, Any]
    installed_by: str = Field(min_length=1, max_length=200)
    enabled: bool = False


async def _install_signal_pack(session: AsyncSession, body: SignalPackIn):
    """`sentinel.packs.install_pack` (upserts on tenant + pack id). The model market
    installs disabled: nothing a stranger shipped fires until switched on."""
    from case_service.sentinel import packs as sentinel_packs

    try:
        row = await sentinel_packs.install_pack(
            session, tenant=body.tenant, manifest=body.manifest, installed_by=body.installed_by)
    except sentinel_packs.ManifestError as exc:
        raise HTTPException(422, str(exc)) from exc
    row.enabled = body.enabled
    await session.flush()
    return {"id": str(row.id)}


@router.post("/order-cases")
async def open_order_case(body: OrderCaseIn, session: AsyncSession = Depends(get_session)):
    return await _open_order_case(session, body)


_local("/order-cases", OrderCaseIn, _open_order_case)

@router.post("/cases/auto-advance")
async def auto_advance(body: CaseIdIn, session: AsyncSession = Depends(get_session)):
    return await _auto_advance(session, body)


_local("/cases/auto-advance", CaseIdIn, _auto_advance)

@router.post("/deploy/apply-bundle")
async def apply_bundle(body: BundleIn, session: AsyncSession = Depends(get_session)):
    return await _apply_bundle(session, body)


_local("/deploy/apply-bundle", BundleIn, _apply_bundle)

@router.post("/engage/rails/validate")
async def validate_rails(body: RailsIn, session: AsyncSession = Depends(get_session)):
    return await _validate_rails(session, body)


_local("/engage/rails/validate", RailsIn, _validate_rails)

@router.post("/engage/rails/upsert-from-pack")
async def upsert_rails(body: RailsUpsertIn, session: AsyncSession = Depends(get_session)):
    return await _upsert_rails(session, body)


_local("/engage/rails/upsert-from-pack", RailsUpsertIn, _upsert_rails)

@router.post("/marketplace/egress-policy")
async def egress_policy(body: EgressPolicyIn, session: AsyncSession = Depends(get_session)):
    return await _egress_policy(session, body)


_local("/marketplace/egress-policy", EgressPolicyIn, _egress_policy)

@router.post("/sentinel/labelled-features")
async def labelled_features(body: TenantIn, session: AsyncSession = Depends(get_session)):
    return await _labelled_features(session, body)


_local("/sentinel/labelled-features", TenantIn, _labelled_features)

@router.post("/sentinel/feature-contexts")
async def feature_contexts(body: FeatureContextsIn, session: AsyncSession = Depends(get_session)):
    return await _feature_contexts(session, body)


_local("/sentinel/feature-contexts", FeatureContextsIn, _feature_contexts)

@router.post("/sentinel/signal-packs")
async def install_signal_pack(body: SignalPackIn, session: AsyncSession = Depends(get_session)):
    return await _install_signal_pack(session, body)


_local("/sentinel/signal-packs", SignalPackIn, _install_signal_pack)

# ── Portal (R5e step 8) — handlers in internal_api/portal.py ──────────────────

def _mount(path: str, model, fn) -> None:
    async def endpoint(body, session=Depends(get_session)):
        return await fn(session, body)

    # Real classes, not strings: this module uses postponed annotations.
    endpoint.__annotations__ = {"body": model, "session": AsyncSession}
    endpoint.__name__ = "internal_" + path.replace("/", "_").replace("-", "_")
    router.add_api_route("/" + path, endpoint, methods=["POST"])
    _local(path, model, fn)


def _mount_portal() -> None:
    from case_service.internal_api import portal as _portal

    for path, (model, fn) in _portal.ROUTES.items():
        _mount(path, model, fn)


_mount_portal()


# ── User (R5e step 10) — handlers in internal_api/user.py ─────────────────────

def _mount_user() -> None:
    from case_service.internal_api import user as _user

    for path, (model, fn) in _user.ROUTES.items():
        _mount(path, model, fn)


_mount_user()
