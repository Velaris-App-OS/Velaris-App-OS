"""HxReplay P3 — resolving indeterminate nodes WITHOUT guessing silently.

When a candidate config makes a never-recorded path reachable (a "lost flow
action"), there is no recorded human outcome to borrow. The resolution ladder
(design §4), each rung clearly labelled, opt-in (run.estimate), and NEVER mixed
into hard determinate metrics:

  1. ``policy_alternative`` — the case's recorded ``decision_point`` audit
     entries already state what POLICY would have done wherever AI decided.
  2. The case-type's policy default — an explicit ``default_outcome`` on the
     stage in ``definition_json`` (an authored default, not an inference).
  3. Monte-Carlo — sample the HISTORICAL remaining-cycle/outcome distribution
     of other cases of the same type that passed through the same stage.
     Deterministically seeded per case (reproducible runs).
  4. Still nothing → the case stays ``indeterminate`` (excluded), as before.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import random
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import CaseAuditLogModel, CaseEventLogModel

MC_SAMPLES = 500
_STATS_CASE_CAP = 500      # cases sampled for the historical distribution

ESTIMATE_LABEL = ("ESTIMATED — outcome substituted or simulated from history, "
                  "not hard replay evidence; reported separately from determinate metrics")


# ── rung 1: recorded policy_alternative ─────────────────────────────────────────

async def fetch_case_decisions(session: AsyncSession, case_id: uuid.UUID) -> list[dict[str, Any]]:
    """The case's recorded decision_point audit entries (oldest first)."""
    rows = (await session.execute(
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.case_id == case_id,
               CaseAuditLogModel.action == "decision_point")
        .order_by(CaseAuditLogModel.timestamp)
    )).scalars().all()
    return [r.details or {} for r in rows]


def match_policy_alternative(decisions: list[dict[str, Any]], ev: dict[str, Any]) -> Any | None:
    """The policy_alternative of an AI decision recorded AT this node, if any.

    Conservative matching: the decision-point name must equal the node's
    stage/step/activity id. Ambiguity (no name match) resolves to None — the
    ladder falls through rather than borrowing the wrong decision.
    """
    node_ids = {v for v in (ev.get("stage_id"), ev.get("step_id"), ev.get("activity")) if v}
    for d in decisions:
        if d.get("source") == "ai" and d.get("policy_alternative") is not None \
                and str(d.get("decision_point")) in node_ids:
            return d["policy_alternative"]
    return None


# ── rung 2: authored case-type default ──────────────────────────────────────────

def policy_default_for(definition_json: dict | None, stage_id: str | None) -> Any | None:
    """An explicit ``default_outcome`` authored on the stage (never inferred)."""
    if not definition_json or not stage_id:
        return None
    for stage in definition_json.get("stages", []) or []:
        if stage.get("id") == stage_id or stage.get("name") == stage_id:
            return stage.get("default_outcome")
    return None


# ── rung 3: historical distribution (Monte-Carlo) ───────────────────────────────

async def stage_remaining_stats(session: AsyncSession, case_type_id, stage_id: str,
                                exclude_case_id=None, tenant_id: str | None = None) -> dict[str, Any]:
    """For OTHER cases of this type that entered ``stage_id``: how long from that
    entry to case end, and how those cases ended. Bounded read.

    Tenant-scoped when ``tenant_id`` is given: the event log has no tenant column,
    so history is restricted via the cases table — another tenant's outcomes must
    never inform this tenant's estimates."""
    q = (select(CaseEventLogModel.case_id, CaseEventLogModel.activity_type,
                CaseEventLogModel.timestamp, CaseEventLogModel.outcome,
                CaseEventLogModel.stage_id)
         .where(CaseEventLogModel.case_type_id == case_type_id,
                CaseEventLogModel.activity_type.in_(("stage_enter", "case_end"))))
    if tenant_id is not None:
        from sqlalchemy import or_
        from case_service.db.models import CaseInstanceModel
        q = q.where(CaseEventLogModel.case_id.in_(
            select(CaseInstanceModel.id).where(
                or_(CaseInstanceModel.tenant_id == tenant_id,
                    CaseInstanceModel.tenant_id.is_(None)))))
    rows = (await session.execute(
        q.order_by(CaseEventLogModel.case_id, CaseEventLogModel.timestamp)
        .limit(_STATS_CASE_CAP * 4)
    )).all()

    entered: dict[Any, Any] = {}
    remaining: list[float] = []
    outcomes: dict[str, int] = {}
    for case_id, atype, ts, outcome, sid in rows:
        if exclude_case_id is not None and case_id == exclude_case_id:
            continue
        if atype == "stage_enter" and sid == stage_id and case_id not in entered:
            entered[case_id] = ts
        elif atype == "case_end" and case_id in entered:
            dt = (ts - entered.pop(case_id)).total_seconds()
            if dt >= 0:
                remaining.append(dt)
                key = str(outcome or "unknown")
                outcomes[key] = outcomes.get(key, 0) + 1
    return {"remaining_seconds": remaining, "outcomes": outcomes, "cases": len(remaining)}


def _pct(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def monte_carlo_estimate(time_to_node_seconds: float, stats: dict[str, Any],
                         seed: int, samples: int = MC_SAMPLES) -> dict[str, Any] | None:
    """Sampled counterfactual cycle time = time-to-node + historical remaining.
    Seeded per case → two runs of the same replay agree (determinism pinning)."""
    remaining = stats.get("remaining_seconds") or []
    if not remaining:
        return None
    rng = random.Random(seed)
    draws = sorted(time_to_node_seconds + rng.choice(remaining) for _ in range(samples))
    total = sum(stats["outcomes"].values()) or 1
    return {
        "cycle_time_seconds": round(sum(draws) / len(draws), 1),
        "cycle_time_p50": _pct(draws, 0.5),
        "cycle_time_p90": _pct(draws, 0.9),
        "outcome_distribution": {k: round(v / total, 4)
                                 for k, v in sorted(stats["outcomes"].items())},
        "samples": samples,
        "history_cases": stats["cases"],
    }


# ── the ladder ──────────────────────────────────────────────────────────────────

async def resolve_lost_node(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    ev: dict[str, Any],
    time_to_node_seconds: float,
    case_type_id=None,
    definition_json: dict | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Try each rung for a newly-reachable node. None → stays indeterminate.

    Returns {"source", "substituted_outcome"?, "metrics"?, "label"} — metrics
    carry ``estimated: True`` and are kept OUT of hard aggregates by callers.
    """
    decisions = await fetch_case_decisions(session, case_id)
    alt = match_policy_alternative(decisions, ev)
    source = None
    outcome = None
    if alt is not None:
        source, outcome = "policy_alternative", alt
    else:
        default = policy_default_for(definition_json, ev.get("stage_id") or ev.get("activity"))
        if default is not None:
            source, outcome = "policy_default", default

    stats = None
    if case_type_id is not None and (ev.get("stage_id") or ev.get("activity")):
        stats = await stage_remaining_stats(
            session, case_type_id, ev.get("stage_id") or ev.get("activity"),
            exclude_case_id=case_id, tenant_id=tenant_id)

    mc = monte_carlo_estimate(time_to_node_seconds, stats or {},
                              seed=int(uuid.UUID(str(case_id)))) if stats else None

    if source is None and mc is None:
        return None
    metrics: dict[str, Any] = {"estimated": True, "source": source or "monte_carlo"}
    if mc:
        metrics.update(mc)
    if outcome is not None:
        metrics["substituted_outcome"] = outcome
    return {"source": metrics["source"], "metrics": metrics, "label": ESTIMATE_LABEL}
