"""HxEvolve — cumulative-drift guardrail (§6).

Every replay proof compares a proposal against the *immediately previous*
config, so a chain of individually-improving merges can compound into a
regression nobody proposed. This module pins a holistic baseline (the
case-type's live metrics when HxEvolve first scanned it) and, after every N
merged HxEvolve changes, compares the CURRENT metrics against it:

- cycle time regressed beyond `min_improvement` (cumulatively), or
- conformance rate dropped more than CONFORMANCE_DROP_POINTS

→ the case-type is FROZEN: scans stop proposing and a `drift` insight is
surfaced. Only an admin can clear it (rebaseline endpoint) — same "AI
proposes, human decides" posture as everything else in HxEvolve.

Evidence here is DESCRIPTIVE (labelled, like routing/sla/reorder proofs): the
merged set can include kinds HxReplay cannot counterfactually replay, so the
honest comparison is live-metrics-then vs live-metrics-now, never a
pseudo-replay.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    ArtifactBranchModel,
    HxEvolveBaselineModel,
    HxEvolveInsightModel,
    _utcnow,
)

logger = logging.getLogger(__name__)

CONFORMANCE_DROP_POINTS = 10.0   # percentage points of conformance loss = drift
MIN_CASES_FOR_COMPARISON = 5     # fewer resolved cases than this = no evidence


async def current_metrics(session: AsyncSession, case_type_id, days: int = 30) -> dict[str, Any]:
    """The case-type's live health from the event log — no replay involved."""
    from case_service.process_mining.analyzer import case_duration_stats, check_conformance

    dur = await case_duration_stats(session, case_type_id=case_type_id, days=days)
    conf = await check_conformance(session, case_type_id, days=days)
    return {
        "window_days": days,
        "cases": dur.get("cases_analyzed", 0),
        "avg_duration_hours": dur.get("avg_duration_hours", 0),
        # analyzer returns conformance_rate as a PERCENTAGE
        "conformance_rate": conf.get("conformance_rate"),
    }


async def merged_change_count(session: AsyncSession, case_type_id) -> int:
    """How many HxEvolve insights for this case type reached a MERGED branch."""
    branch_ids = [
        b for b in (await session.execute(
            select(HxEvolveInsightModel.branch_id).where(
                HxEvolveInsightModel.case_type_id == case_type_id,
                HxEvolveInsightModel.branch_id.isnot(None),
            )
        )).scalars().all()
    ]
    if not branch_ids:
        return 0
    return (await session.execute(
        select(func.count()).select_from(ArtifactBranchModel).where(
            ArtifactBranchModel.id.in_(branch_ids),
            ArtifactBranchModel.status == "merged",
        )
    )).scalar_one()


async def ensure_baseline(
    session: AsyncSession, case_type_id, tenant_id: str, created_by: str | None,
) -> HxEvolveBaselineModel:
    """Lazily pin the baseline the first time HxEvolve looks at a case type."""
    row = await session.get(HxEvolveBaselineModel, case_type_id)
    if row is not None:
        return row
    merged = await merged_change_count(session, case_type_id)
    row = HxEvolveBaselineModel(
        case_type_id=case_type_id, tenant_id=tenant_id,
        metrics=await current_metrics(session, case_type_id),
        merged_at_baseline=merged, checked_through=merged,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


def _problems(baseline: dict, current: dict, min_improvement: float) -> list[str]:
    out: list[str] = []
    if (baseline.get("cases", 0) < MIN_CASES_FOR_COMPARISON
            or current.get("cases", 0) < MIN_CASES_FOR_COMPARISON):
        return out            # too little data on either side = no evidence

    b_dur, c_dur = baseline.get("avg_duration_hours"), current.get("avg_duration_hours")
    if b_dur and c_dur and b_dur > 0:
        rise = (c_dur - b_dur) / b_dur
        if rise > min_improvement:
            out.append(f"mean cycle time regressed {rise:+.0%} vs baseline "
                       f"({b_dur}h -> {c_dur}h)")

    b_conf, c_conf = baseline.get("conformance_rate"), current.get("conformance_rate")
    if b_conf is not None and c_conf is not None:
        drop = b_conf - c_conf
        if drop > CONFORMANCE_DROP_POINTS:
            out.append(f"conformance dropped {drop:.0f} points vs baseline "
                       f"({b_conf:.0f}% -> {c_conf:.0f}%)")
    return out


async def check_drift(
    session: AsyncSession, ct, cfg: dict, *, tenant_id: str, created_by: str | None,
) -> dict[str, Any]:
    """Run before a scan proposes anything.

    Returns {"frozen": bool, "reason": str|None, "insight": model|None} —
    insight is set only when THIS call detected the drift (freshly surfaced)."""
    baseline = await ensure_baseline(session, ct.id, tenant_id, created_by)
    if baseline.frozen:
        return {"frozen": True, "reason": baseline.frozen_reason, "insight": None}

    merged = await merged_change_count(session, ct.id)
    every_n = int(cfg.get("drift_check_every_n_changes", 3) or 3)
    if merged - baseline.checked_through < every_n:
        return {"frozen": False, "reason": None, "insight": None}

    current = await current_metrics(session, ct.id)
    problems = _problems(baseline.metrics or {}, current, cfg.get("min_improvement", 0.10))
    baseline.checked_through = merged
    if not problems:
        logger.info("hxevolve drift check passed for %s (%d merged changes)",
                    ct.id, merged - baseline.merged_at_baseline)
        return {"frozen": False, "reason": None, "insight": None}

    reason = "; ".join(problems)
    baseline.frozen = True
    baseline.frozen_reason = reason
    insight = HxEvolveInsightModel(
        tenant_id=tenant_id, case_type_id=ct.id,
        signal={"kind": "cumulative_drift",
                "merged_changes_since_baseline": merged - baseline.merged_at_baseline},
        proposal={"action": "rebaseline_review",
                  "detail": "Review the merged HxEvolve changes for this case type; "
                            "revert what regressed, then re-baseline to unfreeze."},
        proposal_kind="drift",
        evidence_kind="descriptive",
        evidence={"baseline": baseline.metrics, "current": current,
                  "problems": problems,
                  "policy_alternative": "keep the current configuration and accept "
                                        "the measured cumulative regression"},
        rationale=f"Cumulative drift after {merged - baseline.merged_at_baseline} merged "
                  f"HxEvolve changes: {reason}. Scans are frozen for this case type "
                  f"until an admin re-baselines.",
        status="surfaced",
        created_by=created_by,
    )
    session.add(insight)
    logger.warning("hxevolve drift FROZE case type %s: %s", ct.id, reason)
    return {"frozen": True, "reason": reason, "insight": insight}


async def rebaseline(
    session: AsyncSession, case_type_id, tenant_id: str, updated_by: str | None,
) -> HxEvolveBaselineModel:
    """Admin action: pin a fresh baseline and unfreeze scans."""
    row = await session.get(HxEvolveBaselineModel, case_type_id)
    merged = await merged_change_count(session, case_type_id)
    if row is None:
        row = HxEvolveBaselineModel(case_type_id=case_type_id, tenant_id=tenant_id,
                                    created_by=updated_by, metrics={})
        session.add(row)
    row.metrics = await current_metrics(session, case_type_id)
    row.merged_at_baseline = merged
    row.checked_through = merged
    row.frozen = False
    row.frozen_reason = None
    row.rebaselined_at = _utcnow()
    await session.flush()
    return row


def baseline_view(b: HxEvolveBaselineModel | None) -> dict[str, Any]:
    if b is None:
        return {"exists": False}
    return {
        "exists": True, "metrics": b.metrics,
        "merged_at_baseline": b.merged_at_baseline,
        "checked_through": b.checked_through,
        "frozen": b.frozen, "frozen_reason": b.frozen_reason,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "rebaselined_at": b.rebaselined_at.isoformat() if b.rebaselined_at else None,
    }
