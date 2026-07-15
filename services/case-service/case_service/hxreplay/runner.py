"""HxReplay — run orchestration (P1: single-case, inline).

Loads the baseline rule set for the case's type, builds the candidate set
(HxBranch rule snapshot and/or ad-hoc override), runs the engine, and persists
the outcome to ``replay_runs`` / ``replay_results`` — the ONLY tables replay
ever writes.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db import repository as repo
from case_service.db.models import (
    ArtifactBranchModel,
    CaseInstanceModel,
    ReplayResultModel,
    ReplayRunModel,
)
from case_service.hxreplay import aggregate, anchor, engine

_MAX_RULES = 500
_MAX_COHORT = 5000        # hard cap on cases per cohort run (replica load ceiling)
_DEFAULT_COHORT = 500
_COMMIT_EVERY = 50        # flush cohort results in batches
_MAX_CANDIDATE_RULES = 50  # ad-hoc candidate bounds (admin-only, but still bounded)
_MAX_RULE_ITEMS = 50


class ReplayError(Exception):
    """User-facing replay setup failure (bad branch / candidate shape)."""


def _rule_view(r) -> dict[str, Any]:
    return {"id": str(r.id), "name": r.name, "rule_type": r.rule_type,
            "scope": r.scope, "scope_target_id": r.scope_target_id,
            "definition_json": r.definition_json, "enabled": r.enabled}


async def baseline_rule_set(session: AsyncSession, case_type_id) -> list[dict[str, Any]]:
    """Enabled rules in scope for this case-type: global + case_type-scoped."""
    rules, _total = await repo.list_rules(session, enabled=True, limit=_MAX_RULES)
    out = []
    for r in rules:
        if r.scope == "case_type" and r.scope_target_id \
                and r.scope_target_id != str(case_type_id):
            continue
        out.append(_rule_view(r))
    return out


async def candidate_rule_set(
    session: AsyncSession,
    baseline: list[dict[str, Any]],
    branch_id: uuid.UUID | None,
    candidate: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Baseline with the branch's rule snapshot and/or ad-hoc rules substituted in."""
    by_key = {engine.rule_key(r): dict(r) for r in baseline}

    if branch_id is not None:
        b = await session.get(ArtifactBranchModel, branch_id)
        if b is None:
            raise ReplayError("Branch not found")
        if b.artifact_type != "rule" or not b.content_snapshot:
            raise ReplayError("Branch does not carry a rule snapshot "
                              f"(artifact_type={b.artifact_type!r})")
        snap = dict(b.content_snapshot)
        key = engine.rule_key(snap)
        base = by_key.get(key, {})
        by_key[key] = {**base, **snap}   # keep scope fields from the stored rule

    adhoc = (candidate or {}).get("rules", []) or []
    if len(adhoc) > _MAX_CANDIDATE_RULES:
        raise ReplayError(f"Too many candidate rules (max {_MAX_CANDIDATE_RULES})")
    for r in adhoc:
        if not isinstance(r, dict) or not (r.get("id") or r.get("name")):
            raise ReplayError("Each candidate rule needs an 'id' or 'name'")
        d = r.get("definition_json") or {}
        if len(d.get("conditions") or []) > _MAX_RULE_ITEMS \
                or len(d.get("actions") or []) > _MAX_RULE_ITEMS:
            raise ReplayError(f"Candidate rule too large (max {_MAX_RULE_ITEMS} conditions/actions)")
        key = engine.rule_key(r)
        base = by_key.get(key, {})
        by_key[key] = {**base, **r}

    return list(by_key.values())


async def run_single(session: AsyncSession, run: ReplayRunModel) -> ReplayResultModel:
    """Execute a single-case replay run inline. Raises ReplayError on bad setup."""
    case = await repo.get_case_instance(session, run.case_id)
    if case is None:
        raise ReplayError("Case not found")

    run.status = "running"
    run.started_at = datetime.now(timezone.utc)

    baseline = await baseline_rule_set(session, case.case_type_id)
    candidate = await candidate_rule_set(session, baseline, run.branch_id, run.candidate)

    ct = await repo.get_case_type(session, case.case_type_id)
    outcome = await engine.replay_case(
        session, run.case_id, baseline, candidate,
        case_type_id=case.case_type_id, estimate=bool(run.estimate),
        definition_json=ct.definition_json if ct else None,
        tenant_id=run.tenant_id)

    result = ReplayResultModel(
        run_id=run.id, case_id=run.case_id, tenant_id=run.tenant_id,
        determinacy=outcome["determinacy"],
        exclusion_reason=outcome["exclusion_reason"],
        divergence_point=outcome["divergence_point"],
        baseline_metrics=outcome["baseline_metrics"],
        counterfactual_metrics=outcome["counterfactual_metrics"],
        trace=outcome["trace"],
    )
    session.add(result)

    run.status = "complete"
    run.finished_at = datetime.now(timezone.utc)
    summary = {
        "cases": 1,
        "determinate": 1 if outcome["determinacy"] == "determinate" else 0,
        "estimated": 1 if outcome["determinacy"] == "estimated" else 0,
        "indeterminate": 1 if outcome["determinacy"] == "indeterminate" else 0,
        "divergence_point": outcome["divergence_point"],
        "baseline_metrics": outcome["baseline_metrics"],
        "counterfactual_metrics": outcome["counterfactual_metrics"],
        "assumption": aggregate.ASSUMPTION,
    }
    # cost lookup runs a SELECT (autoflush) — assign summary ONCE, afterwards, or
    # the flushed JSON column would miss an in-place mutation
    summary["cost"] = await _cost_summary(session, run.tenant_id, [outcome])
    run.summary = summary
    await anchor.anchor_run(session, run)
    return result


async def _cost_summary(session: AsyncSession, tenant_id, outcomes) -> dict[str, Any] | None:
    """P4: counterfactual cost delta when the tenant has a rate card (else None)."""
    from case_service.costing import service as costing
    rate = await costing.get_default_rate(session, tenant_id)
    if rate is None:
        return None
    return costing.cost_block(outcomes, rate.hourly_rate, rate.currency)


# ── cohort (Phase C) ────────────────────────────────────────────────────────────

async def select_cohort(session: AsyncSession, tenant_id: str,
                        flt: dict[str, Any]) -> list:
    """Case ids matching the cohort filter (case-type + window, capped, newest first)."""
    try:
        ct_id = uuid.UUID(str(flt["case_type_id"]))
    except (KeyError, ValueError):
        raise ReplayError("cohort_filter.case_type_id (uuid) is required")
    limit = int(flt.get("max_cases") or _DEFAULT_COHORT)
    limit = max(1, min(limit, _MAX_COHORT))

    from sqlalchemy import or_, select as sa_select
    q = sa_select(CaseInstanceModel.id).where(
        CaseInstanceModel.case_type_id == ct_id,
        or_(CaseInstanceModel.tenant_id == tenant_id,
            CaseInstanceModel.tenant_id.is_(None)),
    )
    from case_service.hxreplay.inputs import _aware
    if flt.get("from"):
        q = q.where(CaseInstanceModel.created_at >= _aware(datetime.fromisoformat(str(flt["from"]))))
    if flt.get("to"):
        q = q.where(CaseInstanceModel.created_at <= _aware(datetime.fromisoformat(str(flt["to"]))))
    rows = (await session.execute(
        q.order_by(CaseInstanceModel.created_at.desc()).limit(limit))).scalars().all()
    return list(rows)


async def run_cohort(write_session: AsyncSession, read_session: AsyncSession,
                     run: ReplayRunModel) -> dict[str, Any]:
    """Replay every cohort case; aggregate honestly; anchor. Reads on read_session."""
    flt = run.cohort_filter or {}
    case_ids = await select_cohort(read_session, run.tenant_id or "default", flt)
    if not case_ids:
        raise ReplayError("Cohort filter matched no cases")

    ct_id = uuid.UUID(str(flt["case_type_id"]))
    baseline = await baseline_rule_set(write_session, ct_id)
    candidate = await candidate_rule_set(write_session, baseline, run.branch_id, run.candidate)
    ct = await repo.get_case_type(write_session, ct_id)
    definition_json = ct.definition_json if ct else None

    outcomes: list[dict[str, Any]] = []
    for i, cid in enumerate(case_ids, 1):
        outcome = await engine.replay_case(read_session, cid, baseline, candidate,
                                           case_type_id=ct_id, estimate=bool(run.estimate),
                                           definition_json=definition_json,
                                           tenant_id=run.tenant_id)
        outcomes.append(outcome)
        write_session.add(ReplayResultModel(
            run_id=run.id, case_id=cid, tenant_id=run.tenant_id,
            determinacy=outcome["determinacy"],
            exclusion_reason=outcome["exclusion_reason"],
            divergence_point=outcome["divergence_point"],
            baseline_metrics=outcome["baseline_metrics"],
            counterfactual_metrics=outcome["counterfactual_metrics"],
            trace=outcome["trace"],
        ))
        if i % _COMMIT_EVERY == 0:
            await write_session.commit()

    summary = aggregate.aggregate(outcomes)
    summary["cost"] = await _cost_summary(write_session, run.tenant_id, outcomes)
    run.status = "complete"
    run.finished_at = datetime.now(timezone.utc)
    run.summary = summary
    await anchor.anchor_run(write_session, run, carrier_case_id=case_ids[0])
    return summary


async def bg_run_cohort(run_id) -> None:
    """Background entrypoint. Fresh sessionmakers off the engines — NEVER the
    request-scoped factories (a bg task on the shared test factory corrupts the
    host request; same lesson as testsuite/regen)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from case_service.db.session import get_engine, get_replica_engine

    Write = async_sessionmaker(get_engine(), expire_on_commit=False)
    Read = async_sessionmaker(get_replica_engine(), expire_on_commit=False)
    async with Write() as ws:
        run = await ws.get(ReplayRunModel, run_id)
        if run is None:
            return
        try:
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            await ws.commit()
            async with Read() as rs:
                await run_cohort(ws, rs, run)
            await ws.commit()
        except Exception as exc:
            await ws.rollback()
            run = await ws.get(ReplayRunModel, run_id)
            if run is not None:
                run.status = "failed"
                run.error = str(exc)
                run.finished_at = datetime.now(timezone.utc)
                await ws.commit()
