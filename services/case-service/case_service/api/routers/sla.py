"""SLA API router.

Read SLA status and pause/resume/start SLA clocks.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.cases import SLAStatusResponse
from case_service.core.sla_tracker import start_sla as sla_start, start_sla_v2
from case_service.db import repository as repo
from case_service.db.models import CaseSLAInstanceModel
from case_service.auth.dependencies import get_current_user
from case_service.db.session import get_session

router = APIRouter(tags=["sla"], dependencies=[Depends(get_current_user)])


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
):
    """Return current SLA status for all policies on a case."""
    case = await repo.get_case_instance(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return await repo.get_sla_instances(session, case_id)


@router.post(
    "/cases/{case_id}/sla/start",
    response_model=SLAStatusResponse,
    status_code=201,
)
async def start_sla_endpoint(
    case_id: uuid.UUID,
    body: SLAStartRequest,
    session: AsyncSession = Depends(get_session),
):
    """Start SLA tracking for a case or stage.

    Called by the Temporal workflow activity when entering a stage
    that has an SLA policy configured.
    """
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

    sla = await sla_start(
        session,
        case_id=case_id,
        sla_policy=sla_policy,
        target_id=body.target_id,
    )
    return sla


@router.post(
    "/cases/{case_id}/sla/{policy_id}/pause",
    response_model=SLAStatusResponse,
)
async def pause_sla(
    case_id: uuid.UUID,
    policy_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Pause an SLA clock (e.g. when waiting on an external party)."""
    sla = await _get_sla_or_404(session, case_id, policy_id)
    if sla.status == "paused":
        raise HTTPException(
            status_code=409, detail="SLA is already paused"
        )

    now = datetime.now(timezone.utc)
    await repo.update_sla_instance(
        session,
        sla.id,
        values={"status": "paused", "paused_at": now},
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "sla_paused",
            "actor_type": "system",
            "details": {"sla_policy_id": policy_id},
        },
    )

    return await _get_sla_by_id(session, sla.id)


@router.post(
    "/cases/{case_id}/sla/{policy_id}/resume",
    response_model=SLAStatusResponse,
)
async def resume_sla(
    case_id: uuid.UUID,
    policy_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Resume a paused SLA clock."""
    sla = await _get_sla_or_404(session, case_id, policy_id)
    if sla.status != "paused":
        raise HTTPException(
            status_code=409, detail="SLA is not paused"
        )

    now = datetime.now(timezone.utc)
    paused_seconds = 0
    if sla.paused_at:
        paused_seconds = int((now - sla.paused_at).total_seconds())

    total_paused = sla.paused_duration_seconds + paused_seconds
    await repo.update_sla_instance(
        session,
        sla.id,
        values={
            "status": "on_track",
            "paused_at": None,
            "paused_duration_seconds": total_paused,
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "sla_resumed",
            "actor_type": "system",
            "details": {
                "sla_policy_id": policy_id,
                "paused_seconds": paused_seconds,
            },
        },
    )

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
    session: AsyncSession = Depends(get_session),
):
    """Start SLA v2 — snapshots escalation tree, computes level schedule.

    Called by CaseLifecycleWorkflow via start_sla_v2_tracking activity.
    """
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
    return sla
