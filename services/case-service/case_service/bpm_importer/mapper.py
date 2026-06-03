"""P44 — Pass 3: Map.

Uses the bpm_concepts knowledge base (P42) to translate parsed BPM rules
into Helix-native equivalents. Falls back to keyword heuristics when no
KB entry is found.
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import BpmConceptModel

logger = logging.getLogger(__name__)

# Helix step-type defaults for common BPM step types
_STEP_TYPE_DEFAULTS: dict[str, str] = {
    "user_task":  "user_task",
    "automated":  "automated",
    "approval":   "approval",
    "subprocess": "subprocess",
    "gateway":    "routing",
    "start":      None,
    "end":        None,
}

_CONFIDENCE_ORDER = {"exact": 0, "close": 1, "partial": 2, "manual": 3}


async def map_rules(tool: str, parsed: dict, session: AsyncSession, ai_blueprint: dict | None = None) -> dict:
    """Pass 3: map parsed rules to Helix equivalents.

    Returns:
      { "case_types": [...], "forms": [...], "sla_rules": [...],
        "access_groups": [...], "unmapped": [...] }
    """
    # Load all concepts for this tool from KB once
    kb = await _load_kb(tool, session)

    case_types: list[dict] = []
    forms: list[dict] = []
    sla_rules: list[dict] = []
    access_groups: list[dict] = []
    unmapped: list[dict] = []

    for rule_type, rules in parsed.items():
        for rule in rules:
            _map_rule(tool, rule_type, rule, kb, case_types, forms, sla_rules, access_groups, unmapped)

    # If AI blueprint provided, override round-robin step ordering with AI-inferred ordering
    if ai_blueprint and case_types:
        _apply_blueprint_ordering(case_types, ai_blueprint)

    return {
        "case_types":    case_types,
        "forms":         forms,
        "sla_rules":     sla_rules,
        "access_groups": access_groups,
        "unmapped":      unmapped,
    }


async def _load_kb(tool: str, session: AsyncSession) -> list[dict]:
    rows = (await session.execute(
        select(BpmConceptModel).where(BpmConceptModel.source_tool == tool.lower())
    )).scalars().all()
    return [
        {
            "concept": r.source_concept.lower(),
            "helix_equiv": r.helix_equiv,
            "helix_node_type": r.helix_node_type,
            "confidence": r.confidence,
            "description": r.description,
        }
        for r in rows
    ]


def _kb_lookup(concept: str, kb: list[dict]) -> dict | None:
    """Find best KB match by exact then fuzzy."""
    lower = concept.lower()
    # Exact
    for entry in kb:
        if entry["concept"] == lower:
            return entry
    # Fuzzy
    best, best_score = None, 0.0
    for entry in kb:
        score = SequenceMatcher(None, lower, entry["concept"]).ratio()
        if score > best_score:
            best_score, best = score, entry
    if best and best_score >= 0.6:
        return best
    return None


def _map_rule(
    tool: str,
    rule_type: str,
    rule: dict,
    kb: list[dict],
    case_types: list,
    forms: list,
    sla_rules: list,
    access_groups: list,
    unmapped: list,
) -> None:
    name = rule.get("name", "Unnamed")

    # --- Flow / Process → case_type ---
    if rule_type in ("Flow", "BpmnProcess", "ProcessModel", "Workflow"):
        stages = rule.get("stages", [{"id": "main", "name": "Main"}])
        steps_raw = rule.get("steps", [])
        steps_by_stage = _assign_steps_to_stages(stages, steps_raw)

        ct = {
            "name":        name,
            "source_rule": rule_type,
            "confidence":  "exact",
            "stages": [
                {
                    "id":    s["id"],
                    "name":  s["name"],
                    "steps": steps_by_stage.get(s["id"], []),
                }
                for s in stages
            ],
        }
        case_types.append(ct)
        return

    # --- Section / RecordType → form ---
    if rule_type in ("Section", "RecordType", "Interface", "Harness"):
        fields = rule.get("fields", [])
        kb_entry = _kb_lookup(rule_type, kb)
        forms.append({
            "name":        name,
            "source_rule": rule_type,
            "confidence":  kb_entry["confidence"] if kb_entry else "partial",
            "fields":      fields,
        })
        return

    # --- SLARule → sla_rule ---
    if rule_type == "SLARule":
        sla_rules.append({
            "name":         name,
            "goal_hours":   rule.get("goal_hours", 24),
            "deadline_hours": rule.get("deadline_hours", 48),
            "confidence":   "exact",
        })
        return

    # --- AccessGroup / Group → access_group ---
    if rule_type in ("AccessGroup", "Group"):
        kb_entry = _kb_lookup(rule_type, kb)
        access_groups.append({
            "name":       name,
            "roles":      rule.get("roles", []),
            "confidence": kb_entry["confidence"] if kb_entry else "close",
        })
        return

    # --- Everything else: try KB lookup, else unmapped ---
    kb_entry = _kb_lookup(rule_type, kb) or _kb_lookup(name, kb)
    if kb_entry and _CONFIDENCE_ORDER.get(kb_entry["confidence"], 3) <= 1:
        # Close enough — note it in unmapped with context
        unmapped.append({
            "name": name, "rule_type": rule_type,
            "helix_suggestion": kb_entry["helix_equiv"],
            "confidence": kb_entry["confidence"],
            "needs_review": True,
        })
    else:
        unmapped.append({
            "name": name, "rule_type": rule_type,
            "helix_suggestion": None,
            "confidence": "manual",
            "needs_review": True,
        })


def _apply_blueprint_ordering(case_types: list[dict], ai_blueprint: dict) -> None:
    """Replace round-robin stage/step assignment with AI-inferred ordering.

    Matches parsed case types to blueprint entries by name similarity,
    then replaces stage/step lists with the AI-ordered versions.
    """
    bp_stages = ai_blueprint.get("stages", [])
    if not bp_stages or not case_types:
        return

    # Apply to the first (primary) case type
    ct = case_types[0]
    ct["stages"] = [
        {
            "id":    s.get("stage_key") or s.get("id") or f"stage_{i}",
            "name":  s.get("name", f"Stage {i+1}"),
            "steps": [
                {
                    "name":          st.get("name", "Task"),
                    "step_type":     st.get("step_type", "user_task"),
                    "form_key":      st.get("form_key"),
                    "assignee_type": st.get("assignee_type", "user"),
                    "conditions":    st.get("conditions", []),
                }
                for st in sorted(s.get("steps", []), key=lambda x: x.get("order", 0))
            ],
        }
        for i, s in enumerate(sorted(bp_stages, key=lambda x: x.get("order", 0)))
    ]


def _assign_steps_to_stages(stages: list[dict], steps: list[dict]) -> dict[str, list]:
    """Distribute steps evenly across stages (simple heuristic)."""
    if not stages:
        return {}
    result: dict[str, list] = {s["id"]: [] for s in stages}
    if not steps:
        return result

    # Assign steps to stages in order (round-robin if more steps than stages)
    for i, step in enumerate(steps):
        stage_id = stages[i % len(stages)]["id"]
        result[stage_id].append({
            "name":      step.get("name", "Task"),
            "step_type": step.get("step_type", "user_task"),
        })
    return result
