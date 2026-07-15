"""HxReplay P1 engine — divergence detection + counterfactual recompute.

The determinism split (design §4), applied to one case:

* Baseline = the recorded trace. Never recomputed.
* Only CHANGED rules can diverge (an unchanged deterministic rule over the same
  reconstructed inputs reproduces reality by definition), so the engine diffs the
  baseline rule set against the candidate set and evaluates only the changed ones
  at each stage-entry node, in trace order.
* A changed rule whose conditions touch an input we cannot PROVE decision-time
  values for → the whole case is ``indeterminate`` and excluded (never guessed).
* Divergence classes (closed set):
    - gained **terminal** action  → trace truncated at the node + synthetic
      system ``case_resolved``; everything recorded after is elided.
    - gained **skip** action      → the stage entered at that node is elided
      (its wall-clock span removed); downstream exogenous events held fixed.
    - lost terminal/skip action   → a path that never happened becomes reachable
      (no recorded human outcome to borrow) → ``indeterminate`` (P3 will
      substitute ``policy_alternative``; P1 excludes honestly).
    - anything else (log/notify/set_value/assign) → recorded as a
      non-flow divergence; metrics unchanged.

Pure computation: this module never writes anywhere. Rule evaluation reuses the
production ``rules_evaluator`` (deterministic; expression rules go through
safe_expression inside it).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.core.rules_evaluator import evaluate_rule
from case_service.hxreplay import inputs as inputs_mod
from case_service.hxreplay import trace as trace_mod

_MAX_TRACE_EVENTS = 10_000   # a longer history than this is excluded, not truncated

# action_type / set_value-target classes that alter the FLOW of a case
_TERMINAL_ACTIONS = {"auto_approve", "approve_case", "close_case", "resolve_case"}
_SKIP_ACTIONS = {"advance_stage", "skip_stage", "transition_stage"}
_TERMINAL_STATUS_VALUES = {"resolved", "closed", "approved", "auto_approved"}


# ── rule-set diff ───────────────────────────────────────────────────────────────

def rule_key(rule: dict[str, Any]) -> str:
    return str(rule.get("id") or rule.get("name") or "")


def diff_rule_sets(baseline: list[dict], candidate: list[dict]) -> list[dict[str, Any]]:
    """Rules that differ between the two configs (added / removed / modified)."""
    b = {rule_key(r): r for r in baseline}
    c = {rule_key(r): r for r in candidate}
    changed: list[dict[str, Any]] = []
    for k in sorted(set(b) | set(c)):
        rb, rc = b.get(k), c.get(k)
        if rb is None:
            changed.append({"key": k, "change": "added", "baseline": None, "candidate": rc})
        elif rc is None:
            changed.append({"key": k, "change": "removed", "baseline": rb, "candidate": None})
        elif (rb.get("definition_json") != rc.get("definition_json")
              or bool(rb.get("enabled", True)) != bool(rc.get("enabled", True))):
            changed.append({"key": k, "change": "modified", "baseline": rb, "candidate": rc})
    return changed


# ── input dependencies ──────────────────────────────────────────────────────────

def rule_input_paths(rule: dict[str, Any]) -> set[str]:
    """Every field_path a rule's conditions read (WHEN + decision-table shapes)."""
    d = rule.get("definition_json") or {}
    paths: set[str] = set()
    for cond in d.get("conditions", []) or []:
        for k in ("field_path", "value_field_path"):
            if cond.get(k):
                paths.add(cond[k])
    for col in (d.get("columns") or []):        # decision table
        if col.get("is_condition", True) and col.get("field_path"):
            paths.add(col["field_path"])
    return paths


_P1_RULE_TYPES = ("when", "routing")   # replayable in P1; other types → indeterminate


def _normalise(path: str) -> str:
    # rules context accepts both "crm.x" and "case.data.crm.x" (case_vars façade)
    return path[len("case.data."):] if path.startswith("case.data.") else path


def unresolvable_inputs(changed: list[dict], variables: dict[str, Any],
                        unreconstructable: list[str]) -> set[str]:
    """Inputs of CHANGED rules we cannot prove at decision time.

    A path is resolvable iff it maps to a provably-immutable variable. Expression
    rules (opaque identifier set) and case.* runtime attributes are conservatively
    unresolvable in P1.
    """
    bad: set[str] = set()
    unrecon = set(unreconstructable)
    for ch in changed:
        for rule in (ch["baseline"], ch["candidate"]):
            if rule is None or not rule.get("enabled", True):
                continue
            for p in rule_input_paths(rule):
                n = _normalise(p)
                # case.* runtime attributes (status/stage/…) are time-varying → P2.
                # A path in neither variables nor unrecon never existed on the case
                # → decision-time value was absent (None), which IS deterministic.
                if p.startswith("case.") and not p.startswith("case.data."):
                    bad.add(p)
                elif n in unrecon:
                    bad.add(p)
    return bad


# ── divergence classification ───────────────────────────────────────────────────

def action_class(action: dict[str, Any]) -> str:
    at = (action.get("action_type") or action.get("action") or "").lower()
    if at in _TERMINAL_ACTIONS:
        return "terminal"
    if at in _SKIP_ACTIONS:
        return "skip"
    if at == "set_value":
        tgt = str(action.get("target") or "").lower()
        val = str(action.get("value") or "").lower()
        if tgt in ("case.status", "status") and val in _TERMINAL_STATUS_VALUES:
            return "terminal"
    return "neutral"


def _fired_actions(rules: list[dict], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Actions of the rules that match ``context`` (context never mutated)."""
    fired: list[dict[str, Any]] = []
    for r in rules:
        if not r.get("enabled", True):
            continue
        payload = {"rule_type": r.get("rule_type", "when"), **(r.get("definition_json") or {})}
        try:
            out = evaluate_rule(payload, copy.deepcopy(context))
        except Exception:
            continue    # a rule that errors fires nothing (same as production skip)
        if out.get("matched"):
            for a in (r.get("definition_json") or {}).get("actions", []) or []:
                fired.append(a)
    return fired


# ── the per-case replay ─────────────────────────────────────────────────────────

async def replay_case(
    session: AsyncSession,
    case_id: uuid.UUID,
    baseline_rules: list[dict[str, Any]],
    candidate_rules: list[dict[str, Any]],
    case_type_id=None,
    estimate: bool = False,
    definition_json: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Replay one recorded case against the candidate rule set. Read-only.

    P2: inputs are reconstructed PER NODE by replaying lineage to the node's
    timestamp, so time-varying variables are evaluated with the value the
    decision actually saw. pii/secret fields feed the same '***' constant the
    unprivileged rules façade shows in production (parity, and no PII enters
    the engine). ``case_type_id`` enables that sensitivity resolution.

    P3 (``estimate=True``): a lost-flow-action node (never-recorded path becomes
    reachable) is resolved through the substitution ladder — recorded
    policy_alternative → authored stage default_outcome → Monte-Carlo over the
    stage's historical remaining-cycle distribution. The result is determinacy
    ``estimated``: labelled, and NEVER mixed into hard determinate metrics.
    """
    trace = await trace_mod.load_baseline_trace(session, case_id)
    if len(trace) > _MAX_TRACE_EVENTS:
        # a truncated baseline would silently misrepresent reality — exclude instead
        return _indeterminate(case_id, None, {"event_count": len(trace)}, [],
                              f"event history exceeds the replay limit ({_MAX_TRACE_EVENTS})")
    base_metrics = trace_mod.baseline_metrics(trace)
    if not trace:
        return _indeterminate(case_id, None, base_metrics, trace,
                              "no recorded event history for this case")

    changed = diff_rule_sets(baseline_rules, candidate_rules)
    if not changed:
        return _determinate(case_id, None, base_metrics, base_metrics, trace, trace, [],
                            note="candidate config identical for this case's scope")

    unsupported = sorted({(r or {}).get("rule_type", "when")
                          for ch in changed for r in (ch["baseline"], ch["candidate"])
                          if r and (r.get("rule_type", "when") not in _P1_RULE_TYPES)})
    if unsupported:
        return _indeterminate(
            case_id, None, base_metrics, trace,
            f"changed rules of type {unsupported} are not replayable in P1 "
            f"(supported: {list(_P1_RULE_TYPES)})")

    from datetime import datetime

    # case.* runtime attributes (status/stage/…) are still not reconstructable.
    needed_raw = {p for ch in changed for r in (ch["baseline"], ch["candidate"])
                  if r and r.get("enabled", True) for p in rule_input_paths(r)}
    runtime_attrs = sorted(p for p in needed_raw
                           if p.startswith("case.") and not p.startswith("case.data."))
    if runtime_attrs:
        return _indeterminate(
            case_id, None, base_metrics, trace,
            f"changed rules depend on runtime case attributes {runtime_attrs} "
            "(not reconstructable at decision time)")
    needed = {_normalise(p) for p in needed_raw}

    history = await inputs_mod.load_write_history(session, case_id)
    current_keys = await inputs_mod.current_variable_keys(session, case_id)
    sens = await inputs_mod.rules_visible_sensitivities(session, case_type_id, needed)
    coverage: dict[str, str] = {}

    def _context_at(node_ts: str | None) -> tuple[dict[str, Any] | None, str | None]:
        """(context, None) or (None, why-indeterminate) for one node instant."""
        at = datetime.fromisoformat(node_ts) if node_ts else None
        vars_t: dict[str, Any] = {}
        for p in sorted(needed):
            if sens.get(p) in ("pii", "secret"):
                # production rules read '***' for these at every instant — parity
                vars_t[p] = inputs_mod.REDACTED
                coverage[p] = "constant_redacted"
                continue
            writes = history.get(p)
            if not writes:
                if p in current_keys:
                    coverage[p] = "unknown"
                    return None, (f"input '{p}' has no lineage history — its "
                                  "decision-time value cannot be proven")
                coverage[p] = "absent"      # never existed → deterministic None
                continue
            status, v = inputs_mod.value_at(writes, at) if at else ("unknown", None)
            if status == "value":
                vars_t[p] = v
                coverage[p] = "lineage"
            elif status == "absent":
                coverage[p] = "absent"
            else:
                coverage[p] = "unknown"
                return None, (f"input '{p}' is not reconstructable at decision "
                              "time (pre-capture history or hashed record)")
        # resolve_path tries the remaining path as ONE flat key at each level, so
        # both bare "crm.x" and "case.data.crm.x" resolve against these flat dicts.
        return {**vars_t, "case": {"data": dict(vars_t)}}, None

    changed_baseline = [c["baseline"] for c in changed if c["baseline"]]
    changed_candidate = [c["candidate"] for c in changed if c["candidate"]]

    # Walk stage-entry nodes in trace order; first behavioural difference = divergence.
    for idx, ev in enumerate(trace):
        if ev["activity_type"] not in ("case_start", "stage_enter"):
            continue
        context, why = _context_at(ev.get("timestamp"))
        if context is None:
            return _indeterminate(case_id, ev["activity"], base_metrics, trace, why,
                                  coverage=coverage)
        fired_b = _fired_actions(changed_baseline, context)
        fired_c = _fired_actions(changed_candidate, context)
        if fired_b == fired_c:
            continue

        gained = [a for a in fired_c if a not in fired_b]
        lost = [a for a in fired_b if a not in fired_c]

        if any(action_class(a) in ("terminal", "skip") for a in lost):
            if estimate:
                from case_service.hxreplay import substitute
                t0 = datetime.fromisoformat(trace[0]["timestamp"]) if trace[0].get("timestamp") else None
                tn = datetime.fromisoformat(ev["timestamp"]) if ev.get("timestamp") else None
                resolved = await substitute.resolve_lost_node(
                    session, case_id=case_id, ev=ev,
                    time_to_node_seconds=(tn - t0).total_seconds() if t0 and tn else 0.0,
                    case_type_id=case_type_id, definition_json=definition_json,
                    tenant_id=tenant_id)
                if resolved is not None:
                    return _estimated(case_id, ev["activity"], base_metrics, trace,
                                      resolved, coverage=coverage)
            return _indeterminate(
                case_id, ev["activity"], base_metrics, trace,
                "candidate config makes a previously-skipped path reachable — "
                "no recorded outcome to borrow"
                + ("; no substitution source found" if estimate
                   else " (enable estimation to substitute policy/history)"),
                coverage=coverage)

        gained_terminal = next((a for a in gained if action_class(a) == "terminal"), None)
        gained_skip = next((a for a in gained if action_class(a) == "skip"), None)

        if gained_terminal is not None:
            cf = _truncate_at(trace, idx, gained_terminal)
        elif gained_skip is not None:
            target = str(gained_skip.get("value") or gained_skip.get("target") or "") or None
            j = _find_stage_node(trace, idx, target)
            if j is None:
                return _determinate(case_id, ev["activity"], base_metrics, base_metrics,
                                    trace, trace, gained + lost,
                                    note="skip target stage is not on this case's recorded path",
                                    coverage=coverage)
            cf = _elide_stage(trace, j)
        else:
            # non-flow divergence: same shape, different side-effects
            return _determinate(case_id, ev["activity"], base_metrics, base_metrics,
                                trace, trace, gained + lost,
                                note="divergence has no flow effect (notification/value only)",
                                coverage=coverage)

        kept = [n for n in cf if not n.get("_elided")]
        cf_metrics = trace_mod.baseline_metrics(kept)
        # Exogenous timestamps are held fixed (never shifted), so for a skipped
        # stage the saving shows as elided wall-clock, not a shorter cycle time.
        cf_metrics["elided_wall_seconds"] = _elided_wall_seconds(cf)
        return _determinate(case_id, ev["activity"], base_metrics, cf_metrics,
                            trace, cf, gained, note=None, coverage=coverage)

    return _determinate(case_id, None, base_metrics, base_metrics, trace, trace, [],
                        note="changed rules never fire differently on this case's inputs",
                        coverage=coverage)


# ── counterfactual trace builders ───────────────────────────────────────────────

def _truncate_at(trace: list[dict], idx: int, action: dict) -> list[dict]:
    cf = [{**e, "_class": "copied"} for e in trace[: idx + 1]]
    cf.append({
        "id": None, "activity": "case_resolved", "activity_type": "case_end",
        "stage_id": trace[idx].get("stage_id"), "step_id": None,
        "actor_id": "hxreplay", "actor_type": "system",
        "timestamp": trace[idx]["timestamp"], "duration_seconds": 0,
        "outcome": str(action.get("action_type") or action.get("value") or "auto_resolved"),
        "metadata": {}, "_class": "synthetic",
    })
    cf.extend({**e, "_class": "elided", "_elided": True} for e in trace[idx + 1:])
    return cf


def _find_stage_node(trace: list[dict], from_idx: int, stage_id: str | None) -> int | None:
    """Index of the stage_enter node for ``stage_id`` (or the next stage if None)."""
    for j in range(from_idx, len(trace)):
        if trace[j]["activity_type"] == "stage_enter" and \
                (stage_id is None or trace[j].get("stage_id") == stage_id
                 or trace[j].get("activity") == stage_id):
            return j
    return None


def _elide_stage(trace: list[dict], idx: int) -> list[dict]:
    """Elide the stage entered at ``idx`` (up to the next stage entry / case end)."""
    end = next((j for j in range(idx + 1, len(trace))
                if trace[j]["activity_type"] in ("stage_enter", "case_end")), len(trace))
    cf: list[dict] = []
    for j, e in enumerate(trace):
        if idx <= j < end:
            cf.append({**e, "_class": "elided", "_elided": True})
        else:
            cf.append({**e, "_class": "copied"})
    return cf


def _elided_wall_seconds(cf: list[dict]) -> float:
    """Wall-clock span covered by the elided block(s) — the time the change removes."""
    from datetime import datetime
    total = 0.0
    block: list[dict] = []
    for e in cf + [{"_elided": False, "timestamp": None}]:
        if e.get("_elided"):
            block.append(e)
        elif block:
            ts = [datetime.fromisoformat(b["timestamp"]) for b in block if b.get("timestamp")]
            if len(ts) >= 2:
                total += (max(ts) - min(ts)).total_seconds()
            elif len(ts) == 1 and block[-1].get("duration_seconds"):
                total += float(block[-1]["duration_seconds"])
            block = []
    return total


# ── result shells ───────────────────────────────────────────────────────────────

def _determinate(case_id, divergence, base_m, cf_m, base_trace, cf_trace, actions, note,
                 coverage=None):
    return {
        "case_id": str(case_id), "determinacy": "determinate",
        "divergence_point": divergence, "exclusion_reason": None,
        "baseline_metrics": base_m, "counterfactual_metrics": cf_m,
        "trace": {"nodes": cf_trace, "diverging_actions": actions, "note": note,
                  "input_coverage": coverage or {}},
    }


def _indeterminate(case_id, divergence, base_m, base_trace, reason, coverage=None):
    return {
        "case_id": str(case_id), "determinacy": "indeterminate",
        "divergence_point": divergence, "exclusion_reason": reason,
        "baseline_metrics": base_m, "counterfactual_metrics": None,
        "trace": {"nodes": [{**e, "_class": "copied"} for e in base_trace],
                  "diverging_actions": [], "note": reason,
                  "input_coverage": coverage or {}},
    }


def _estimated(case_id, divergence, base_m, base_trace, resolved, coverage=None):
    """P3: substituted/simulated outcome — labelled, kept out of hard metrics."""
    return {
        "case_id": str(case_id), "determinacy": "estimated",
        "divergence_point": divergence, "exclusion_reason": None,
        "baseline_metrics": base_m,
        "counterfactual_metrics": resolved["metrics"],
        "trace": {"nodes": [{**e, "_class": "copied"} for e in base_trace],
                  "diverging_actions": [],
                  "note": f"{resolved['label']} (source: {resolved['source']})",
                  "input_coverage": coverage or {}},
    }
