"""SLA API router.

Read SLA status and pause/resume/start SLA clocks.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.cases import SLAStatusResponse
from case_service.core import sla_tracker
from case_service.core.sla_tracker import start_sla as sla_start, start_sla_v2
from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db import repository as repo
from velaris_shared.db.models import CaseSLAInstanceModel
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.db.session import get_session

router = APIRouter(tags=["sla"], dependencies=[Depends(get_current_user)])


async def _signal_companion(request: Request, case_id: uuid.UUID) -> None:
    """Best-effort wake of the case's SLA companion workflow."""
    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        return
    try:
        handle = client.get_workflow_handle(f"helix-case-{case_id}")
        await handle.signal("sla_refresh", {})
    except Exception:
        pass  # companion rescans periodically; the nudge is optional


class SLAStartRequest(BaseModel):
    sla_policy_id: str
    target_id: str


@router.get(
    "/cases/{case_id}/sla",
    response_model=list[SLAStatusResponse],
)
async def get_case_sla_status(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return current SLA status for all policies on a case."""
    # Was authentication only until 2026-09-29: another tenant's case now 404s.
    await hxguard.require_case(session, user, "case.read", case_id)
    case = await repo.get_case_instance(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return await repo.get_sla_instances(session, case_id)


@router.get("/cases/{case_id}/sla/prediction")
async def get_case_sla_prediction(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Statistical breach risk for one case (roadmap #40, §4.5).

    Percentile arithmetic over this case type's own history — no model, no
    training. Reports the case grain and the current-stage grain separately,
    plus a single `at_risk` flag for callers that do not want to reason about
    grains. Either grain reporting `insufficient_data` is a real answer, not
    an error: a percentile drawn from a handful of cases misleads.
    """
    # Was authentication only until 2026-09-29: another tenant's case now 404s.
    await hxguard.require_case(session, user, "case.read", case_id)
    from case_service.core import sla_prediction

    try:
        return await sla_prediction.predict_for_case(session, case_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/case-types/{case_type_id}/sla/distribution")
async def get_case_type_sla_distribution(
    case_type_id: uuid.UUID,
    stage_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """The historical distribution a prediction is drawn from.

    Exposed so a designer can see *why* a case was flagged — and, when there
    is not enough history, see that plainly instead of wondering why nothing
    is ever flagged.
    """
    from case_service.core import sla_prediction

    samples = (
        await sla_prediction.stage_durations(session, case_type_id, stage_id)
        if stage_id else
        await sla_prediction.case_type_durations(session, case_type_id)
    )
    out = {
        "case_type_id": str(case_type_id),
        "stage_id": stage_id,
        "grain": "stage" if stage_id else "case",
        "samples": len(samples),
        "min_samples": sla_prediction.MIN_SAMPLES,
        "sufficient": len(samples) >= sla_prediction.MIN_SAMPLES,
    }
    if samples:
        out |= {
            "median_seconds": round(sla_prediction.value_at_percentile(samples, 50.0), 2),
            "p85_seconds": round(sla_prediction.value_at_percentile(samples, 85.0), 2),
            "p95_seconds": round(sla_prediction.value_at_percentile(samples, 95.0), 2),
            "min_seconds": round(samples[0], 2),
            "max_seconds": round(samples[-1], 2),
        }
    return out


@router.post(
    "/cases/{case_id}/sla/start",
    response_model=SLAStatusResponse,
    status_code=201,
)
async def start_sla_endpoint(
    case_id: uuid.UUID,
    body: SLAStartRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Start SLA tracking for a case or stage (manual/API path)."""
    # Was authentication only until 2026-09-29: another tenant's case now 404s.
    await hxguard.require_case(session, user, "case.read", case_id)
    case = await repo.get_case_instance(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    # Load the case type to find the SLA policy definition
    case_type = await repo.get_case_type(session, case.case_type_id)
    if case_type is None:
        raise HTTPException(status_code=404, detail="Case type not found")

    definition = case_type.definition_json or {}
    sla_policies = definition.get("sla_policies", [])

    sla_policy = next(
        (s for s in sla_policies if s["id"] == body.sla_policy_id),
        None,
    )
    if sla_policy is None:
        raise HTTPException(
            status_code=404,
            detail=f"SLA policy '{body.sla_policy_id}' not found in case type definition",
        )

    # One instance per (case, policy) — a case's SLAs start when it is created,
    # so a second start hit the unique key and answered 500 (R5b drill, 2026-10-30).
    already = (await session.execute(select(CaseSLAInstanceModel.id).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == body.sla_policy_id,
    ))).scalar_one_or_none()
    if already is not None:
        raise HTTPException(status_code=409, detail="This SLA is already running for this case")

    sla = await sla_start(
        session,
        case_id=case_id,
        sla_policy=sla_policy,
        target_id=body.target_id,
    )
    await _signal_companion(request, case_id)
    return sla


@router.post(
    "/cases/{case_id}/sla/{policy_id}/pause",
    response_model=SLAStatusResponse,
)
async def pause_sla(
    case_id: uuid.UUID,
    policy_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Pause an SLA clock (e.g. when waiting on an external party)."""
    # Was authentication only until 2026-09-29: another tenant's case now 404s.
    await hxguard.require_case(session, user, "case.read", case_id)
    sla = await _get_sla_or_404(session, case_id, policy_id)
    if sla.status == "paused":
        raise HTTPException(
            status_code=409, detail="SLA is already paused"
        )

    paused = await sla_tracker.pause_sla(
        session, case_id=case_id, sla_policy_id=policy_id
    )
    if not paused:
        raise HTTPException(
            status_code=409, detail="SLA is not in a pausable state"
        )

    await _signal_companion(request, case_id)
    return await _get_sla_by_id(session, sla.id)


@router.post(
    "/cases/{case_id}/sla/{policy_id}/resume",
    response_model=SLAStatusResponse,
)
async def resume_sla(
    case_id: uuid.UUID,
    policy_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Resume a paused SLA clock.

    Delegates to sla_tracker.resume_sla, which shifts goal_at/deadline_at
    forward by the paused duration — pausing actually stops the clock.
    """
    # Was authentication only until 2026-09-29: another tenant's case now 404s.
    await hxguard.require_case(session, user, "case.read", case_id)
    sla = await _get_sla_or_404(session, case_id, policy_id)
    if sla.status != "paused":
        raise HTTPException(
            status_code=409, detail="SLA is not paused"
        )

    resumed = await sla_tracker.resume_sla(
        session, case_id=case_id, sla_policy_id=policy_id
    )
    if not resumed:
        raise HTTPException(status_code=409, detail="SLA could not be resumed")

    await _signal_companion(request, case_id)
    return await _get_sla_by_id(session, sla.id)


# ─── Helpers ──────────────────────────────────────────────────────────


async def _get_sla_or_404(
    session: AsyncSession,
    case_id: uuid.UUID,
    policy_id: str,
) -> CaseSLAInstanceModel:
    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == policy_id,
    )
    result = await session.execute(stmt)
    sla = result.scalar_one_or_none()
    if sla is None:
        raise HTTPException(
            status_code=404, detail="SLA instance not found"
        )
    return sla


async def _get_sla_by_id(
    session: AsyncSession, sla_id: uuid.UUID
) -> CaseSLAInstanceModel:
    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.id == sla_id
    )
    result = await session.execute(stmt)
    return result.scalar_one()



@router.post(
    "/cases/{case_id}/sla/start-v2",
    response_model=SLAStatusResponse,
    status_code=201,
)
async def start_sla_v2_endpoint(
    case_id: uuid.UUID,
    body: SLAStartRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Start SLA v2 — snapshots escalation tree, computes level schedule."""
    # Was authentication only until 2026-09-29: another tenant's case now 404s.
    await hxguard.require_case(session, user, "case.read", case_id)
    case = await repo.get_case_instance(session, case_id)
    if case is None:
        raise HTTPException(404, "Case not found")
    case_type = await repo.get_case_type(session, case.case_type_id)
    if case_type is None:
        raise HTTPException(404, "Case type not found")

    definition = case_type.definition_json or {}
    policies = definition.get("sla_policies", [])
    sla_policy = next((s for s in policies if s["id"] == body.sla_policy_id), None)
    if sla_policy is None:
        raise HTTPException(404, f"SLA policy '{body.sla_policy_id}' not in case type")

    tenant_id = getattr(case, "tenant_id", None)
    sla = await start_sla_v2(
        session,
        case_id=case_id,
        case_type_id=case.case_type_id,
        sla_policy=sla_policy,
        target_id=body.target_id,
        tenant_id=tenant_id,
    )
    await _signal_companion(request, case_id)
    return sla
