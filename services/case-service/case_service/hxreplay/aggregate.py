"""HxReplay — cohort aggregation: honest deltas over the determinate subset.

The trust numbers ship WITH the deltas (design §4): determinacy coverage, the
exclusion-reason histogram, and the selection-bias caveat — the excluded cases
skew toward exactly the cases the change affects most, so the determinate-subset
deltas can be biased, not merely lower-coverage.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

BIAS_CAVEAT = (
    "Excluded (indeterminate) cases are NOT a random sample — they skew toward the "
    "cases the change affects most. Deltas over the determinate subset may be biased; "
    "review the exclusion profile before acting on this result."
)
ASSUMPTION = "exogenous (human/external) events held fixed from the record"


def _estimated_block(est: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Labelled aggregate over P3-estimated cases — separate from hard metrics."""
    if not est:
        return None
    from case_service.hxreplay.substitute import ESTIMATE_LABEL
    cycles = [m["cycle_time_seconds"] for m in (o["counterfactual_metrics"] for o in est)
              if m and m.get("cycle_time_seconds") is not None]
    sources: dict[str, int] = {}
    outcome_dist: dict[str, float] = {}
    for o in est:
        m = o["counterfactual_metrics"] or {}
        sources[m.get("source", "unknown")] = sources.get(m.get("source", "unknown"), 0) + 1
        for k, v in (m.get("outcome_distribution") or {}).items():
            outcome_dist[k] = outcome_dist.get(k, 0.0) + v
    n = len(est)
    return {
        "cases": n,
        "cycle_time": {"mean": round(sum(cycles) / len(cycles), 1) if cycles else None,
                       "p50": _pct(cycles, 0.5), "p90": _pct(cycles, 0.9)},
        "sources": sources,
        "outcome_distribution": {k: round(v / n, 4) for k, v in sorted(outcome_dist.items())},
        "label": ESTIMATE_LABEL,
    }


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[idx]


def _cycle_stats(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    cycles = [m["cycle_time_seconds"] for m in metrics
              if m and m.get("cycle_time_seconds") is not None]
    return {
        "mean": round(sum(cycles) / len(cycles), 1) if cycles else None,
        "p50": _pct(cycles, 0.5),
        "p90": _pct(cycles, 0.9),
    }


def aggregate(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Cohort summary over per-case engine outcomes.

    Hard metrics come from the determinate subset ONLY. P3 ``estimated`` cases
    (substituted/simulated outcomes) are aggregated in their own labelled block
    and never blended in.
    """
    det = [o for o in outcomes if o["determinacy"] == "determinate"]
    est = [o for o in outcomes if o["determinacy"] == "estimated"]
    ind = [o for o in outcomes if o["determinacy"] == "indeterminate"]
    diverged = [o for o in det if o["divergence_point"] is not None]

    reasons: dict[str, int] = {}
    for o in ind:
        key = (o.get("exclusion_reason") or "unknown").split(":")[0][:120]
        reasons[key] = reasons.get(key, 0) + 1

    base_m = [o["baseline_metrics"] for o in det]
    cf_m = [o["counterfactual_metrics"] or o["baseline_metrics"] for o in det]

    def _ratio(ms):
        rs = [m["auto_ratio"] for m in ms if m and m.get("auto_ratio") is not None]
        return round(sum(rs) / len(rs), 4) if rs else None

    total = len(outcomes)
    return {
        "cases": total,
        "determinate": len(det),
        "estimated": len(est),
        "indeterminate": len(ind),
        "coverage_ratio": round(len(det) / total, 4) if total else None,
        "estimated_block": _estimated_block(est),
        "diverged": len(diverged),
        "divergence_rate": round(len(diverged) / len(det), 4) if det else None,
        "exclusion_profile": {"reasons": reasons},
        "baseline": {"cycle_time": _cycle_stats(base_m), "auto_ratio": _ratio(base_m)},
        "counterfactual": {
            "cycle_time": _cycle_stats(cf_m), "auto_ratio": _ratio(cf_m),
            "elided_wall_seconds_total": round(sum(
                (m or {}).get("elided_wall_seconds", 0) or 0 for m in cf_m), 1),
        },
        "assumption": ASSUMPTION,
        "bias_caveat": BIAS_CAVEAT,
    }
