"""Case Costing API — rate card + automatic time rollup (HxReplay P4 / §11).

Rates are commercially sensitive: gated behind the dedicated HxGuard capability
``costing.rates.access`` (admin), per-tenant, never reachable by portal identities.
The time rollup for a case inherits that case's view authorization.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from case_service.costing import service as costing
from case_service.tenancy.identity import same_tenant
from velaris_shared.db import repository as repo
from velaris_shared.config import get_settings
from velaris_shared.db.session import get_session
from velaris_shared.db.read_routing import get_cutoff_session

router = APIRouter(prefix="/costing", tags=["costing"])


def _tenant(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


@router.get("/rate-card")
async def get_rate_card(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await hxguard.require(session, hxguard.subject_from_user(user), "costing.rates.access")
    row = await costing.get_default_rate(session, _tenant(user))
    if row is None:
        return {"configured": False, "hourly_rate": None, "currency": "USD"}
    return {"configured": True, "hourly_rate": row.hourly_rate, "currency": row.currency,
            "updated_at": (row.updated_at or row.created_at).isoformat()
            if (row.updated_at or row.created_at) else None}


class RateCardBody(BaseModel):
    hourly_rate: float = Field(ge=0, le=1_000_000)
    currency: str = Field(default="USD", min_length=1, max_length=8)


@router.put("/rate-card")
async def put_rate_card(
    body: RateCardBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await hxguard.require(session, hxguard.subject_from_user(user), "costing.rates.access")
    row = await costing.upsert_default_rate(session, _tenant(user),
                                            body.hourly_rate, body.currency, user.user_id)
    await session.commit()
    return {"configured": True, "hourly_rate": row.hourly_rate, "currency": row.currency}


@router.get("/cases/{case_id}/time")
async def case_time(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Automatic time rollup from recorded event durations (zero new capture)."""
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(404, "Case not found")
    case = await repo.get_case_instance(session, cid)
    # `case.tenant_id` is a GUID and `_tenant(user)` is a SLUG, so this compare
    # was never equal for a tenant-owned case and every one of them answered
    # "Case not found" — to its OWN tenant. Proven on 2026-09-06: the `default`
    # user could not read the time recorded on the `default` tenant's own case.
    # It failed closed, so it was never an exposure, but time tracking and
    # billing were simply unavailable on every tenanted case. `same_tenant`
    # accepts both conventions and is what the rest of the platform uses.
    if case is None or not await same_tenant(session, case.tenant_id, user.tenant_id):
        raise HTTPException(404, "Case not found")
    await hxguard.require_case(session, user, "case.read", cid)
    return await costing.case_time_rollup(session, cid)


# ── §11 P2: manual timers / timesheets ────────────────────────────────
# Effort capture is case work: start/stop/add require case.update, reading
# requires case.read — same posture as every case action (404 anti-oracle).

async def _authorized_case(session, user, case_id: str, action: str) -> uuid.UUID:
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(404, "Case not found")
    case = await repo.get_case_instance(session, cid)
    # `case.tenant_id` is a GUID and `_tenant(user)` is a SLUG, so this compare
    # was never equal for a tenant-owned case and every one of them answered
    # "Case not found" — to its OWN tenant. Proven on 2026-09-06: the `default`
    # user could not read the time recorded on the `default` tenant's own case.
    # It failed closed, so it was never an exposure, but time tracking and
    # billing were simply unavailable on every tenanted case. `same_tenant`
    # accepts both conventions and is what the rest of the platform uses.
    if case is None or not await same_tenant(session, case.tenant_id, user.tenant_id):
        raise HTTPException(404, "Case not found")
    await hxguard.require_case(session, user, action, cid)
    return cid


def _entry_view(e) -> dict:
    return {
        "id": str(e.id), "case_id": str(e.case_id), "user_id": e.user_id,
        "role": e.role, "source": e.source, "billable": e.billable,
        "running": e.source == "timer" and e.ended_at is None,
        "started_at": e.started_at.isoformat() if e.started_at else None,
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
        "duration_seconds": e.duration_seconds, "note": e.note,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


class TimerStartBody(BaseModel):
    role: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/cases/{case_id}/timer/start", status_code=201)
async def start_timer(
    case_id: str,
    body: TimerStartBody | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    cid = await _authorized_case(session, user, case_id, "case.update")
    if await costing.running_timer(session, cid, str(user.user_id)) is not None:
        raise HTTPException(409, "A timer is already running for you on this case")
    body = body or TimerStartBody()
    entry = await costing.start_timer(
        session, tenant_id=_tenant(user), case_id=cid,
        user_id=str(user.user_id), role=body.role, note=body.note)
    await session.commit()
    await session.refresh(entry)
    return _entry_view(entry)


@router.post("/cases/{case_id}/timer/stop")
async def stop_timer(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    cid = await _authorized_case(session, user, case_id, "case.update")
    entry = await costing.running_timer(session, cid, str(user.user_id))
    if entry is None:
        raise HTTPException(404, "No running timer for you on this case")
    await costing.stop_timer(session, entry)
    await session.commit()
    await session.refresh(entry)
    return _entry_view(entry)


class TimeEntryBody(BaseModel):
    duration_seconds: int = Field(ge=1, le=24 * 3600)
    role: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    billable: bool = True


@router.post("/cases/{case_id}/time-entries", status_code=201)
async def add_time_entry(
    case_id: str,
    body: TimeEntryBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Direct timesheet entry — effort logged after the fact."""
    from velaris_shared.db.models import CaseTimeEntryModel
    cid = await _authorized_case(session, user, case_id, "case.update")
    entry = CaseTimeEntryModel(
        tenant_id=_tenant(user), case_id=cid, user_id=str(user.user_id),
        role=body.role, source="timesheet", duration_seconds=body.duration_seconds,
        billable=body.billable, note=body.note)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return _entry_view(entry)


@router.get("/cases/{case_id}/time-entries")
async def list_time_entries(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    cid = await _authorized_case(session, user, case_id, "case.read")
    entries = await costing.list_entries(session, cid)
    rollup = await costing.entries_rollup(session, _tenant(user), cid)
    return {"items": [_entry_view(e) for e in entries], "rollup": rollup}


@router.delete("/time-entries/{entry_id}", status_code=204)
async def delete_time_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Own entries only (admins included — effort claims belong to their author)."""
    from velaris_shared.db.models import CaseTimeEntryModel
    entry = await session.get(CaseTimeEntryModel, entry_id)
    if entry is None or entry.user_id != str(user.user_id):
        raise HTTPException(404, "Time entry not found")   # anti-oracle
    await _authorized_case(session, user, str(entry.case_id), "case.update")
    # Who spent how long on a held case is part of its record (Oct item 13).
    from case_service.legal_hold import service as _legal_hold
    await _legal_hold.require_not_held(session, entry.case_id, "changed by deleting a time entry")
    await session.delete(entry)
    await session.commit()


# ── §11 P3: billing export ────────────────────────────────────────────

@router.get("/export")
async def export_billing(
    case_type_id: str | None = None,
    days: int = 30,
    fmt: str = "json",
    session: AsyncSession = Depends(get_cutoff_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Per-case billable cost lines for invoicing (HxCheckout / storefront).

    Per-case cost is commercially sensitive — same HxGuard capability as the
    rate card (``costing.rates.access``), never reachable by portal identities."""
    from datetime import datetime, timedelta, timezone
    await hxguard.require(session, hxguard.subject_from_user(user), "costing.rates.access")

    ct_id: uuid.UUID | None = None
    if case_type_id:
        try:
            ct_id = uuid.UUID(case_type_id)
        except ValueError:
            raise HTTPException(404, "Case type not found")
    days = max(1, min(days, 366))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    lines = await costing.billing_export(
        session, _tenant(user), case_type_id=ct_id, since=since)

    # HxPrivacy (§8.10): this export leaves the tenant for invoicing, so
    # confirmed personal data is masked before it does. Only CONFIRMED
    # findings redact — a heuristic guess must not blank a billing column.
    # Off by default; enabling it is a deliberate act.
    if get_settings().pii_redact_exports:
        from velaris_shared.db import pii_redaction as _pii

        _paths = await _pii.confirmed_field_paths(session, tenant_id=_tenant(user))
        lines = _pii.redact_rows(lines, _paths)

    if fmt == "csv":
        from fastapi.responses import PlainTextResponse
        cols = ["case_id", "case_number", "case_type_id", "entries",
                "billable_seconds", "billable_cost", "currency", "fully_priced"]
        rows = [",".join(cols)] + [
            ",".join("" if l[c] is None else str(l[c]) for c in cols) for l in lines]
        return PlainTextResponse("\n".join(rows) + "\n", media_type="text/csv")
    return {"window_days": days, "lines": lines, "count": len(lines)}


# ══ F-02 — explainable cost per decision ════════════════════════════════════
#
# Appended to this router rather than given its own, because it is the same
# subject: what work costs. The rate card above prices human hours; these price
# model tokens, and `/cases/{id}/cost` is the one place the two are reconciled.
#
# The gates differ from the ones above, deliberately. `costing.rates.access` is
# about the LABOUR rate; setting what a model is charged at is its own action,
# because on a shared install the platform-wide default row reaches every tenant
# that has not priced that model for itself.

async def _tenant_uuid_for_costing(session: AsyncSession, user: AuthenticatedUser):
    """The tenant's UUID. `_tenant()` above returns the SLUG — `rate_cards` keys
    on it — while the F-02 tables key on the id, because they cascade with the
    tenant row and sit beside the ledger."""
    from case_service.tenancy.identity import resolve_tenant

    resolved = await resolve_tenant(session, _tenant(user))
    if resolved is None:
        raise HTTPException(403, "VEL-COST-TENANT-UNRESOLVED-403")
    return resolved[0]


class ModelRateBody(BaseModel):
    model_config = {"extra": "forbid", "protected_namespaces": ()}

    model_id: str = Field(min_length=1, max_length=255)
    input_per_mtok: float = Field(ge=0, le=1_000_000)
    output_per_mtok: float = Field(ge=0, le=1_000_000)
    currency: str = Field(default="USD", min_length=1, max_length=8)
    #: Set a PLATFORM default that every unpriced tenant inherits. Reserved to a
    #: platform operator below — one tenant's admin must not be able to price
    #: models for the whole box.
    platform: bool = False


@router.get("/model-rates")
async def list_model_rates(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("costing.model_rates.access")),
):
    """Every model rate that applies here, with inherited defaults marked."""
    from case_service.costing import decisions as dcost

    tenant = await _tenant_uuid_for_costing(session, user)
    return {"rates": await dcost.list_rates(session, tenant_id=tenant)}


@router.put("/model-rates")
async def put_model_rate(
    body: ModelRateBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("costing.model_rates.access")),
):
    from case_service.costing import decisions as dcost
    from case_service.tenancy.identity import actor_uuid, is_superadmin

    if body.platform and not is_superadmin(user):
        # The same guard HxArbiter puts on a platform-scoped policy, for the
        # same reason: on a shared install "admin" is a TENANT admin, and a
        # platform default row is inherited by every tenant that has not priced
        # that model itself.
        raise HTTPException(
            403,
            "VEL-COST-PLATFORM-SCOPE-403: a platform rate is inherited by "
            "every tenant and is reserved to a platform operator.")

    tenant = None if body.platform else await _tenant_uuid_for_costing(session, user)
    try:
        from case_service.costing import decisions as dcost
        row = await dcost.upsert_rate(
            session, tenant_id=tenant, model_id=body.model_id,
            input_per_mtok=body.input_per_mtok,
            output_per_mtok=body.output_per_mtok,
            currency=body.currency,
            # `model_rate_cards.created_by` is a GUID and `user.user_id` is a
            # username for an SSO or service principal, so the raw value made
            # this route answer 422 for everybody who did not sign in with a
            # password. Resolve both forms; an unknown principal is absent.
            created_by=await actor_uuid(session, user))
    except dcost.CostingError as exc:
        raise HTTPException(422, f"VEL-COST-INVALID-422: {exc}") from exc
    await session.commit()
    return {
        "model_id": row.model_id,
        "input_per_mtok": float(row.input_per_mtok),
        "output_per_mtok": float(row.output_per_mtok),
        "currency": row.currency,
        "scope": "platform" if body.platform else "tenant",
    }


@router.get("/cases/{case_id}/cost")
async def case_cost(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("costing.decisions.read")),
):
    """What one case cost, as line items that add up to the total.

    Both halves: the AI lines are stored, the labour line is derived from the
    recorded durations and the tenant's hourly rate. `labour_included` says so
    rather than leaving a reader to assume.
    """
    from case_service.costing import decisions as dcost

    tenant = await _tenant_uuid_for_costing(session, user)
    return await dcost.case_breakdown(
        session, tenant_id=tenant, case_id=case_id, tenant_slug=_tenant(user))


@router.get("/rollup")
async def cost_rollup(
    days: int = 30,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("costing.decisions.read")),
):
    """Cost by model and by kind across the tenant."""
    from datetime import datetime, timedelta, timezone

    from case_service.costing import decisions as dcost

    days = max(1, min(int(days), 3650))
    tenant = await _tenant_uuid_for_costing(session, user)
    return await dcost.tenant_rollup(
        session, tenant_id=tenant,
        since=datetime.now(timezone.utc) - timedelta(days=days))
