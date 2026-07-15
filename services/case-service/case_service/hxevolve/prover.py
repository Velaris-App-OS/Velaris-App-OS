"""HxEvolve §3.3 Prove — the gate that makes it safe.

Rule-kind proposals are proved by an **HxReplay cohort counterfactual** run
synchronously in the scan, then judged by VETO guardrails (anti-Goodhart, §3.3):
a proposal is surfaced only if it improves the objective by the minimum AND
regresses no guardrail AND the cohort was trustworthy. Anything else is
discarded with the reason recorded — a human never sees it as a suggestion.

Routing / SLA / reorder proposals CANNOT be replay-proved (the engine
counterfactuals rule sets, not assignment, SLA clocks or step order — §8.3), so
their evidence is **descriptive** mining statistics, labelled as such and judged
only for plausibility, never presented as counterfactual proof.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import ReplayRunModel
from case_service.hxreplay import runner
from case_service.process_mining import analyzer

logger = logging.getLogger(__name__)

# ── veto threshold DEFAULTS (P3: per-case-type overrides via hxevolve_config) ────
MIN_IMPROVEMENT = 0.10        # objective must improve ≥10% (mean cycle time)
MAX_AUTO_RATIO_RISE = 0.15    # anti-"auto-approve everything" ceiling (absolute)
MIN_COVERAGE = 0.7            # determinacy coverage below this = untrustworthy
MIN_DETERMINATE = 50          # cohort must have at least this many determinate cases
COHORT_MAX_CASES = 300        # bounded synchronous run inside the scan request


def _thresholds(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = config or {}
    return {
        "min_improvement": cfg.get("min_improvement", MIN_IMPROVEMENT),
        "max_auto_ratio_rise": cfg.get("max_auto_ratio_rise", MAX_AUTO_RATIO_RISE),
        "min_coverage": cfg.get("min_coverage", MIN_COVERAGE),
        "min_determinate": cfg.get("min_determinate", MIN_DETERMINATE),
    }

REPLAY_KINDS = ("rule_adjust", "rule_add")
DESCRIPTIVE_KINDS = ("sla_duration", "routing", "reorder")


async def prove(session: AsyncSession, *, kind: str, proposal: dict[str, Any],
                case_type_id: uuid.UUID, tenant_id: str,
                candidate_signal: dict[str, Any],
                created_by: str | None,
                config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Judge one gated proposal.

    Returns ``{verdict, evidence_kind, evidence, replay_run_id}`` where verdict ∈
    {surfaced, discarded_guardrail, insufficient_evidence}.
    """
    if kind in REPLAY_KINDS:
        return await _prove_by_replay(session, proposal, case_type_id, tenant_id,
                                      created_by, config)
    return await _describe(session, kind, proposal, case_type_id,
                           candidate_signal)


# ── counterfactual proof (rule kinds) ────────────────────────────────────────────

async def _prove_by_replay(session: AsyncSession, proposal: dict[str, Any],
                           case_type_id: uuid.UUID, tenant_id: str,
                           created_by: str | None,
                           config: dict[str, Any] | None = None) -> dict[str, Any]:
    # parity with the manual cohort endpoint: one running cohort per tenant —
    # a busy tenant yields an honest insufficient-evidence verdict, never a queue
    from sqlalchemy import select
    busy = (await session.execute(
        select(ReplayRunModel.id).where(
            ReplayRunModel.tenant_id == tenant_id,
            ReplayRunModel.kind == "cohort",
            ReplayRunModel.status.in_(("pending", "running")),
        ).limit(1)
    )).scalar_one_or_none()
    if busy is not None:
        return {"verdict": "insufficient_evidence", "evidence_kind": "counterfactual",
                "evidence": {"error": "a cohort replay is already running for this "
                                      "tenant — re-scan when it finishes"},
                "replay_run_id": None}

    run = ReplayRunModel(
        tenant_id=tenant_id, kind="cohort",
        cohort_filter={"case_type_id": str(case_type_id),
                       "max_cases": COHORT_MAX_CASES},
        candidate={"rules": [proposal]},
        created_by=created_by or "hxevolve",
    )
    session.add(run)
    try:
        summary = await runner.run_cohort(session, session, run)
    except runner.ReplayError as exc:
        run.status = "failed"
        run.error = str(exc)
        return {"verdict": "insufficient_evidence", "evidence_kind": "counterfactual",
                "evidence": {"error": str(exc)}, "replay_run_id": run.id}

    evidence = _evidence_block(summary)
    verdict, reasons = _apply_vetoes(summary, config)
    evidence["veto_reasons"] = reasons
    return {"verdict": verdict, "evidence_kind": "counterfactual",
            "evidence": evidence, "replay_run_id": run.id}


def _evidence_block(summary: dict[str, Any]) -> dict[str, Any]:
    base = (summary.get("baseline") or {})
    cf = (summary.get("counterfactual") or {})
    return {
        "cases": summary.get("cases"),
        "determinate": summary.get("determinate"),
        "coverage_ratio": summary.get("coverage_ratio"),
        "baseline_cycle_time": base.get("cycle_time"),
        "counterfactual_cycle_time": cf.get("cycle_time"),
        "baseline_auto_ratio": base.get("auto_ratio"),
        "counterfactual_auto_ratio": cf.get("auto_ratio"),
        "divergence_rate": summary.get("divergence_rate"),
        "cost": summary.get("cost"),
        "assumption": summary.get("assumption"),
    }


def _apply_vetoes(summary: dict[str, Any],
                  config: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """Guardrails are VETOES, not soft penalties (§3.3)."""
    t = _thresholds(config)
    reasons: list[str] = []

    determinate = summary.get("determinate") or 0
    coverage = summary.get("coverage_ratio") or 0.0
    if determinate < t["min_determinate"]:
        reasons.append(f"only {determinate} determinate case(s) "
                       f"(min {t['min_determinate']}) — not statistically meaningful")
    if coverage < t["min_coverage"]:
        reasons.append(f"determinacy coverage {coverage} below {t['min_coverage']}")
    if reasons:
        return "insufficient_evidence", reasons

    base_mean = ((summary.get("baseline") or {}).get("cycle_time") or {}).get("mean")
    cf_mean = ((summary.get("counterfactual") or {}).get("cycle_time") or {}).get("mean")
    if not base_mean or cf_mean is None:
        return "insufficient_evidence", ["no measurable cycle-time delta"]
    improvement = (base_mean - cf_mean) / base_mean
    if improvement < t["min_improvement"]:
        reasons.append(f"objective improvement {improvement:.1%} is below the "
                       f"{t['min_improvement']:.0%} minimum")

    base_auto = (summary.get("baseline") or {}).get("auto_ratio") or 0.0
    cf_auto = (summary.get("counterfactual") or {}).get("auto_ratio")
    if cf_auto is not None and (cf_auto - base_auto) > t["max_auto_ratio_rise"]:
        reasons.append(f"auto-ratio would rise {cf_auto - base_auto:+.2f} "
                       f"(ceiling +{t['max_auto_ratio_rise']}) — auto-approval "
                       f"gaming veto")

    cost = summary.get("cost") or {}
    delta = cost.get("delta") if isinstance(cost, dict) else None
    if isinstance(delta, (int, float)) and delta > 0:
        reasons.append(f"cost would regress by {delta} — cost guardrail veto")

    return ("discarded_guardrail", reasons) if reasons else ("surfaced", [])


# ── descriptive evidence (routing / sla / reorder — labelled, never "proof") ─────

async def _describe(session: AsyncSession, kind: str, proposal: dict[str, Any],
                    case_type_id: uuid.UUID,
                    candidate_signal: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "note": ("Descriptive evidence from process-mining statistics — HxReplay "
                 "cannot counterfactual assignment, SLA clocks or step ordering; "
                 "this is NOT a simulated proof."),
        "signal": candidate_signal,
    }
    if kind == "sla_duration":
        evidence["duration_stats"] = await analyzer.case_duration_stats(
            session, case_type_id, days=int(candidate_signal.get("window_days", 30)))
    elif kind == "routing":
        step = (proposal.get("step_id") or "")
        bottlenecks = await analyzer.find_bottlenecks(
            session, case_type_id, days=int(candidate_signal.get("window_days", 30)))
        evidence["step_stats"] = next(
            (b for b in bottlenecks if b["activity"] == step), None)
    elif kind == "reorder":
        evidence["estimate"] = _parallelization_estimate(
            proposal.get("definition_json") or {},
            await analyzer.find_bottlenecks(
                session, case_type_id,
                days=int(candidate_signal.get("window_days", 30)), limit=50))
    return {"verdict": "surfaced", "evidence_kind": "descriptive",
            "evidence": evidence, "replay_run_id": None}


def _parallelization_estimate(definition: dict[str, Any],
                              bottlenecks: list[dict[str, Any]]) -> dict[str, Any]:
    """Naive labelled estimate: for stages proposed parallel, sequential time is
    the SUM of step averages, parallel time is the MAX — the saving is the
    difference. Purely arithmetic on observed averages."""
    by_activity = {b["activity"]: b["avg_duration_seconds"] for b in bottlenecks}
    saved = 0.0
    for stage in definition.get("stages", []):
        if isinstance(stage, dict) and stage.get("stage_type") == "parallel":
            durations = [by_activity.get(st.get("id"), 0.0)
                         for st in stage.get("steps", []) if isinstance(st, dict)]
            durations = [d for d in durations if d > 0]
            if len(durations) >= 2:
                saved += sum(durations) - max(durations)
    return {"estimated_saving_seconds_per_case": round(saved, 1),
            "method": "sum-vs-max of observed step averages (naive, no dependency "
                      "analysis)"}
