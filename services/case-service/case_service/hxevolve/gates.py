"""HxEvolve §3.2 gates — the structural gate HxDraft doesn't have.

Rule / SLA / routing proposals re-validate through the HxDraft gates
(`rule_builder`, `sla_builder`, `routing_builder`). The fifth mutation kind —
step reorder / parallelize (signed off §8.3) — gets this PERMUTATION-ONLY gate:

  * the stage-id set must be identical;
  * every stage keeps the same step-id multiset — nothing added, removed or
    renamed anywhere;
  * each step object must be BYTE-IDENTICAL to its current self (only its
    position may change);
  * a stage may change only its `order`, its step sequence, and its
    `stage_type` between 'linear' and 'parallel';
  * every other key of the definition (forms, variables, sla_policies,
    notifications, …) must be byte-identical.

Anything else fails closed. AI output is data, never instructions.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
from typing import Any

_STAGE_MUTABLE = {"order", "steps", "stage_type"}
_STAGE_TYPES = {"linear", "parallel"}


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def validate_reorder(current: dict[str, Any], proposed: Any) -> list[str]:
    """Every reason the proposed definition is NOT a pure reorder. Empty = ok."""
    errors: list[str] = []
    if not isinstance(proposed, dict):
        return ["Proposed definition is not an object"]

    # everything outside `stages` must be untouched
    cur_rest = {k: v for k, v in (current or {}).items() if k != "stages"}
    new_rest = {k: v for k, v in proposed.items() if k != "stages"}
    if _canon(cur_rest) != _canon(new_rest):
        changed = sorted(set(cur_rest) ^ set(new_rest)
                         | {k for k in set(cur_rest) & set(new_rest)
                            if _canon(cur_rest[k]) != _canon(new_rest[k])})
        errors.append(f"A reorder may only touch stages — these keys changed: "
                      f"{changed}")

    cur_stages = {s.get("id"): s for s in (current or {}).get("stages", [])
                  if isinstance(s, dict)}
    new_stages_list = [s for s in proposed.get("stages", []) if isinstance(s, dict)]
    new_stages = {s.get("id"): s for s in new_stages_list}
    if len(new_stages) != len(new_stages_list):
        errors.append("Duplicate stage ids in the proposed definition")
    if set(cur_stages) != set(new_stages):
        errors.append(f"Stage ids must be identical (a reorder never adds or "
                      f"removes stages): {sorted(set(cur_stages) ^ set(new_stages))}")
        return errors

    for sid, cur_stage in cur_stages.items():
        new_stage = new_stages[sid]
        tag = f"Stage {sid!r}"

        # only order / steps-sequence / stage_type may differ
        cur_fixed = {k: v for k, v in cur_stage.items() if k not in _STAGE_MUTABLE}
        new_fixed = {k: v for k, v in new_stage.items() if k not in _STAGE_MUTABLE}
        if _canon(cur_fixed) != _canon(new_fixed):
            errors.append(f"{tag}: only step order, stage order and stage_type may "
                          f"change in a reorder")
        st = new_stage.get("stage_type", cur_stage.get("stage_type", "linear"))
        if st not in _STAGE_TYPES:
            errors.append(f"{tag}: stage_type must be one of {sorted(_STAGE_TYPES)}")

        cur_steps = [s for s in cur_stage.get("steps", []) if isinstance(s, dict)]
        new_steps = [s for s in new_stage.get("steps", []) if isinstance(s, dict)]
        cur_by_id = {s.get("id"): s for s in cur_steps}
        new_by_id = {s.get("id"): s for s in new_steps}
        if len(new_by_id) != len(new_steps):
            errors.append(f"{tag}: duplicate step ids")
            continue
        if set(cur_by_id) != set(new_by_id):
            errors.append(f"{tag}: step ids must be identical (nothing added, "
                          f"removed or renamed): "
                          f"{sorted(set(cur_by_id) ^ set(new_by_id))}")
            continue
        for step_id, cur_step in cur_by_id.items():
            if _canon(cur_step) != _canon(new_by_id[step_id]):
                errors.append(f"{tag}: step {step_id!r} was modified — a reorder "
                              f"may only move steps, never change them")
    return errors
