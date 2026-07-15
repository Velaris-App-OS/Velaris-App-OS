"""HxReplay API — P1 (single-case counterfactual replay).

Fork a REAL recorded case against a candidate rule config (HxBranch snapshot or
ad-hoc override) and show the counterfactual side-by-side with reality. Replay is
read-only over case history; its only writes are replay_runs / replay_results.

Authorization: creating a single-case replay inherits the authz of viewing that
case (it exposes that case's history); cohort replay (Phase C) is a bulk read
gated behind the dedicated HxGuard capability ``replay.run``.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import hxguard
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db import repository as repo
from case_service.db.models import ReplayResultModel, ReplayRunModel
from case_service.db.session import get_session
from case_service.hxreplay import runner

router = APIRouter(prefix="/hxreplay", tags=["hxreplay"])


def _tenant(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


def _is_admin(user: AuthenticatedUser) -> bool:
    roles = user.roles or []
    return user.has_privilege("*", "*") or "admin" in roles or "superadmin" in roles


def _run_view(r: ReplayRunModel, *, with_summary: bool = True) -> dict:
    v = {
        "id": str(r.id), "kind": r.kind, "status": r.status,
        "case_id": str(r.case_id) if r.case_id else None,
        "branch_id": str(r.branch_id) if r.branch_id else None,
        "cohort_filter": r.cohort_filter or {},
        "config_epoch": r.config_epoch,
        "estimate": r.estimate,
        "anchored": r.anchored, "error": r.error, "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }
    if with_summary:
        v["summary"] = r.summary
    return v


def _result_view(res: ReplayResultModel, *, with_trace: bool = False) -> dict:
    v = {
        "id": str(res.id), "case_id": str(res.case_id),
        "determinacy": res.determinacy, "exclusion_reason": res.exclusion_reason,
        "divergence_point": res.divergence_point,
        "baseline_metrics": res.baseline_metrics,
        "counterfactual_metrics": res.counterfactual_metrics,
    }
    if with_trace:
        v["trace"] = res.trace
    return v


async def _get_run(session: AsyncSession, user: AuthenticatedUser, run_id: str) -> ReplayRunModel:
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(404, "Run not found")
    r = (await session.execute(
        select(ReplayRunModel).where(ReplayRunModel.id == rid,
                                     ReplayRunModel.tenant_id == _tenant(user))
    )).scalar_one_or_none()
    # anti-oracle: other tenant / other creator → same 404
    if r is None or not (_is_admin(user) or r.created_by == user.user_id):
        raise HTTPException(404, "Run not found")
    return r


class RunCreate(BaseModel):
    kind: str = "single"                            # single | cohort
    case_id: Optional[str] = None                   # single runs
    cohort_filter: dict = Field(default_factory=dict)   # {case_type_id, from, to, max_cases}
    branch_id: Optional[str] = None
    candidate: dict = Field(default_factory=dict)   # {"rules": [{id|name, definition_json, ...}]}
    estimate: bool = False                          # P3: substitute/simulate lost nodes (labelled)


def _parse_branch(body: RunCreate) -> uuid.UUID | None:
    if not body.branch_id:
        return None
    try:
        return uuid.UUID(body.branch_id)
    except ValueError:
        raise HTTPException(404, "Branch not found")


@router.post("/runs", status_code=201)
async def create_run(
    body: RunCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if body.kind == "single":
        return await _create_single(body, session, user)
    if body.kind == "cohort":
        return await _create_cohort(body, background_tasks, session, user)
    raise HTTPException(400, "kind must be 'single' or 'cohort'")


async def _create_single(body: RunCreate, session: AsyncSession, user: AuthenticatedUser):
    try:
        case_id = uuid.UUID(body.case_id or "")
    except ValueError:
        raise HTTPException(404, "Case not found")
    case = await repo.get_case_instance(session, case_id)
    if case is None or (case.tenant_id is not None and case.tenant_id != _tenant(user)):
        raise HTTPException(404, "Case not found")
    # single-case replay shows this case's real history → same authz as viewing it
    await hxguard.require_case(session, user, "case.read", case_id)
    # Ad-hoc candidate rules are arbitrary conditions evaluated server-side (incl.
    # regex operators) — an authoring capability, admin-only like the rules CRUD.
    # Non-admins may still replay against a governed HxBranch rule snapshot.
    if body.candidate and not _is_admin(user):
        raise HTTPException(403, "Ad-hoc candidate rules require admin; use branch_id")

    run = ReplayRunModel(
        tenant_id=_tenant(user), kind="single", case_id=case_id,
        branch_id=_parse_branch(body), candidate=body.candidate or {},
        estimate=body.estimate, created_by=user.user_id,
    )
    session.add(run)
    try:
        result = await runner.run_single(session, run)
    except runner.ReplayError as exc:
        run.status = "failed"
        run.error = str(exc)
        await session.commit()
        raise HTTPException(400, str(exc))
    await session.commit()
    await session.refresh(run)
    return {**_run_view(run), "result": _result_view(result, with_trace=True)}


async def _create_cohort(body: RunCreate, background_tasks: BackgroundTasks,
                         session: AsyncSession, user: AuthenticatedUser):
    # bulk read of many real cases → dedicated HxGuard capability, not a generic gate
    await hxguard.require(session, hxguard.subject_from_user(user), "replay.run",
                          resource={"cohort": body.cohort_filter})
    if not (body.cohort_filter or {}).get("case_type_id"):
        raise HTTPException(400, "cohort_filter.case_type_id is required")

    # per-tenant concurrency cap: one running cohort at a time
    busy = (await session.execute(
        select(ReplayRunModel.id).where(
            ReplayRunModel.tenant_id == _tenant(user),
            ReplayRunModel.kind == "cohort",
            ReplayRunModel.status.in_(("pending", "running")),
        ).limit(1)
    )).scalar_one_or_none()
    if busy is not None:
        raise HTTPException(409, "A cohort replay is already running for this tenant")

    run = ReplayRunModel(
        tenant_id=_tenant(user), kind="cohort", cohort_filter=body.cohort_filter,
        branch_id=_parse_branch(body), candidate=body.candidate or {},
        estimate=body.estimate, created_by=user.user_id,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    background_tasks.add_task(runner.bg_run_cohort, run.id)
    return _run_view(run)


@router.get("/runs")
async def list_runs(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    q = select(ReplayRunModel).where(ReplayRunModel.tenant_id == _tenant(user))
    if not _is_admin(user):
        q = q.where(ReplayRunModel.created_by == user.user_id)
    rows = (await session.execute(q.order_by(desc(ReplayRunModel.created_at)).limit(100))).scalars().all()
    return {"runs": [_run_view(r, with_summary=False) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return _run_view(await _get_run(session, user, run_id))


@router.get("/runs/{run_id}/results")
async def run_results(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    r = await _get_run(session, user, run_id)
    rows = (await session.execute(
        select(ReplayResultModel).where(ReplayResultModel.run_id == r.id)
        .order_by(ReplayResultModel.created_at)
    )).scalars().all()
    return {"results": [_result_view(x) for x in rows]}


@router.get("/runs/{run_id}/results/{case_id}")
async def run_result_detail(
    run_id: str,
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    r = await _get_run(session, user, run_id)
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(404, "Result not found")
    res = (await session.execute(
        select(ReplayResultModel).where(ReplayResultModel.run_id == r.id,
                                        ReplayResultModel.case_id == cid)
    )).scalars().first()
    if res is None:
        raise HTTPException(404, "Result not found")
    return _result_view(res, with_trace=True)


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    r = await _get_run(session, user, run_id)
    await session.delete(r)     # replay_results cascade via FK
    await session.commit()
