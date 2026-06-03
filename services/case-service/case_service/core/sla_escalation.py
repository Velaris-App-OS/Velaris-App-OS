"""SLA escalation engine.

Tree schema (canonical):
{
  "levels": [
    {
      "level": 1,
      "name": "Primary assignee reminder",
      "trigger": {
        "type": "goal_pct",         // "goal_pct" | "deadline_pct" | "fixed_duration"
        "value": 80                 // 80% of goal time, or "PT1H" for fixed
      },
      "actions": [
        {"type": "notify", "target_type": "current_assignee"},
        {"type": "reassign", "target_type": "queue", "target_id": "managers"},
        {"type": "priority", "set": "high"}
      ]
    },
    { "level": 2, ... },
    ...
  ]
}

Performance design:
- Tree is JSON-evaluated (no DB joins at breach time)
- Snapshot copied onto SLA instance at start → decouples from tree mutations
- All level timers pre-computed into absolute datetimes when SLA starts
"""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.core.sla_tracker import parse_iso8601_duration
from case_service.db import repository as repo
from case_service.db.models import (
    CaseSLAInstanceModel, EscalationTreeModel,
    CaseAssignmentModel, CaseInstanceModel,
)

log = logging.getLogger(__name__)


# ═══ Tree resolution ═══════════════════════════════════════════════════

async def resolve_escalation_tree(
    session: AsyncSession,
    case_type_id: Optional[uuid.UUID],
    tenant_id: Optional[str] = None,
) -> Optional[EscalationTreeModel]:
    """Find the most specific active tree for a case type.

    Priority: case-type-specific > global. Tenant-scoped wins within same level.
    """
    q = (
        select(EscalationTreeModel)
        .where(EscalationTreeModel.is_active.is_(True))
        .where(EscalationTreeModel.case_type_id == case_type_id)
    )
    if tenant_id:
        q = q.where(
            (EscalationTreeModel.tenant_id == tenant_id)
            | (EscalationTreeModel.tenant_id.is_(None))
        )
    res = await session.execute(q)
    tree = res.scalars().first()
    if tree:
        return tree

    # Fall back to global
    q2 = (
        select(EscalationTreeModel)
        .where(EscalationTreeModel.is_active.is_(True))
        .where(EscalationTreeModel.scope == "global")
        .where(EscalationTreeModel.case_type_id.is_(None))
    )
    if tenant_id:
        q2 = q2.where(
            (EscalationTreeModel.tenant_id == tenant_id)
            | (EscalationTreeModel.tenant_id.is_(None))
        )
    res2 = await session.execute(q2)
    return res2.scalars().first()


def snapshot_tree(tree: EscalationTreeModel) -> dict:
    """Serialize tree to a snapshot dict (so later edits don't affect in-flight SLAs)."""
    return {
        "tree_id": str(tree.id),
        "name": tree.name,
        "levels": tree.tree_json.get("levels", []),
    }


# ═══ Trigger computation ═══════════════════════════════════════════════

def compute_level_trigger_at(
    level: dict,
    started_at: datetime,
    goal_at: datetime,
    deadline_at: datetime,
) -> Optional[datetime]:
    """Return the absolute datetime a level fires at, or None if invalid."""
    trigger = level.get("trigger", {}) or {}
    t_type = trigger.get("type")
    value = trigger.get("value")

    if t_type == "goal_pct":
        try:
            pct = float(value) / 100.0
        except (TypeError, ValueError):
            return None
        delta = (goal_at - started_at).total_seconds() * pct
        return started_at + timedelta(seconds=delta)

    if t_type == "deadline_pct":
        try:
            pct = float(value) / 100.0
        except (TypeError, ValueError):
            return None
        delta = (deadline_at - started_at).total_seconds() * pct
        return started_at + timedelta(seconds=delta)

    if t_type == "fixed_duration":
        try:
            return started_at + parse_iso8601_duration(str(value))
        except ValueError:
            return None

    if t_type == "at_breach":
        return deadline_at

    return None


def precompute_level_schedule(
    snapshot: dict,
    started_at: datetime,
    goal_at: datetime,
    deadline_at: datetime,
) -> list[dict]:
    """Return [{level, fires_at, name, actions}, ...] sorted by fires_at."""
    out = []
    for lvl in snapshot.get("levels", []):
        fires_at = compute_level_trigger_at(lvl, started_at, goal_at, deadline_at)
        if fires_at is None:
            continue
        out.append({
            "level": lvl.get("level", 0),
            "name": lvl.get("name", f"Level {lvl.get('level', 0)}"),
            "fires_at": fires_at.isoformat(),
            "actions": lvl.get("actions", []),
        })
    out.sort(key=lambda x: x["fires_at"])
    return out


# ═══ Action executor ═══════════════════════════════════════════════════

async def resolve_dynamic_target(
    session: AsyncSession,
    case_id: uuid.UUID,
    target_type: str,
    target_id: Optional[str],
    tenant_id: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve a target_type/target_id into a concrete (effective_type, effective_id).

    Dynamic types resolve at-fire-time:
      - current_assignee → user + current assignee's user_id
      - manager_of_current_assignee → user + manager's user_id
      - access_group  → queue-like (lands in group's pool)  (id passed through)
      - role          → queue-like (id passed through as role label)
      - queue / user  → passed through unchanged
    Returns (effective_type, effective_id). If resolution fails returns ("", "")
    so the caller can mark the action failed.
    """
    from case_service.core.user_directory import (
        get_current_assignee_for_case, get_manager,
    )
    t = (target_type or "").lower()

    if t == "current_assignee":
        user_id = await get_current_assignee_for_case(session, case_id)
        return ("user", user_id) if user_id else ("", "")

    if t == "manager_of_current_assignee":
        user_id = await get_current_assignee_for_case(session, case_id)
        if not user_id:
            return ("", "")
        mgr = await get_manager(session, user_id)
        return ("user", mgr) if mgr else ("", "")

    if t == "access_group":
        return ("queue", f"access_group:{target_id}") if target_id else ("", "")

    if t == "role":
        return ("queue", f"role:{target_id}") if target_id else ("", "")

    # Pass-through types: user, queue, or whatever was configured
    return (t or "queue", target_id or "")


async def execute_action(
    session: AsyncSession,
    case_id: uuid.UUID,
    action: dict,
    sla: CaseSLAInstanceModel,
) -> dict:
    """Execute a single escalation action. Returns a result dict for audit."""
    atype = action.get("type")
    result = {"action": atype, "ok": True, "detail": None}

    try:
        if atype == "notify":
            target_type = action.get("target_type", "current_assignee")
            target_id = action.get("target_id")
            eff_type, eff_id = await resolve_dynamic_target(
                session, case_id, target_type, target_id,
            )
            if not eff_id:
                result["ok"] = False
                result["detail"] = f"could not resolve notify target: {target_type}"
                return result
            result["detail"] = {
                "target_type": eff_type, "target_id": eff_id,
                "message": action.get("message") or f"SLA escalation: case {case_id}",
            }

        elif atype == "reassign":
            target_type = action.get("target_type", "queue")
            target_id = action.get("target_id")
            eff_type, eff_id = await resolve_dynamic_target(
                session, case_id, target_type, target_id,
            )
            if not eff_id:
                result["ok"] = False
                result["detail"] = (
                    f"could not resolve reassign target: {target_type}" +
                    (f" (id={target_id})" if target_id else "")
                )
                return result
            # Reassign all active assignments for the case
            q = select(CaseAssignmentModel).where(
                CaseAssignmentModel.case_id == case_id,
                CaseAssignmentModel.status == "active",
            )
            res = await session.execute(q)
            count = 0
            for a in res.scalars().all():
                prev = a.assignee_id
                a.assignee_id = eff_id
                a.assignee_type = eff_type
                a.claimed_at = None
                count += 1
                await repo.append_audit_entry(
                    session, data={
                        "case_id": case_id,
                        "action": "assignment_reassigned",
                        "actor_type": "system",
                        "details": {
                            "assignment_id": str(a.id),
                            "from": prev, "to": eff_id,
                            "target_type": eff_type,
                            "reason": "sla_escalation",
                            "original_target_type": target_type,
                        },
                    },
                )
            await session.flush()
            result["detail"] = {
                "reassigned_count": count,
                "to": eff_id,
                "target_type": eff_type,
                "resolved_from": target_type,
            }

        elif atype == "priority":
            new_priority = action.get("set", "high")
            case = await session.get(CaseInstanceModel, case_id)
            if case:
                case.priority = new_priority
                await session.flush()
            result["detail"] = {"priority": new_priority}

        elif atype == "status":
            new_status = action.get("set")
            if new_status:
                case = await session.get(CaseInstanceModel, case_id)
                if case:
                    case.status = new_status
                    await session.flush()
            result["detail"] = {"status": new_status}

        else:
            result["ok"] = False
            result["detail"] = f"unknown action type: {atype}"
    except Exception as e:
        log.exception("action %s failed", atype)
        result["ok"] = False
        result["detail"] = str(e)
    return result


async def apply_level(
    session: AsyncSession,
    case_id: uuid.UUID,
    sla: CaseSLAInstanceModel,
    level_entry: dict,
) -> dict:
    """Execute all actions for one escalation level; update sla state."""
    results = []
    for action in level_entry.get("actions", []):
        r = await execute_action(session, case_id, action, sla)
        results.append(r)

    # Update SLA with escalation history + current level
    now = datetime.now(timezone.utc)
    history = list(sla.escalation_history or [])
    history.append({
        "level": level_entry.get("level"),
        "name": level_entry.get("name"),
        "fired_at": now.isoformat(),
        "actions": results,
    })
    sla.escalation_level = int(level_entry.get("level", sla.escalation_level))
    sla.escalation_history = history
    await session.flush()

    await repo.append_audit_entry(
        session, data={
            "case_id": case_id,
            "action": "sla_escalated",
            "actor_type": "system",
            "details": {
                "sla_policy_id": sla.sla_policy_id,
                "level": sla.escalation_level,
                "actions_applied": len(results),
                "actions_ok": sum(1 for r in results if r["ok"]),
            },
        },
    )

    return {
        "level": sla.escalation_level,
        "actions": results,
        "fired_at": now.isoformat(),
    }


# ═══ Pause tracking ═══════════════════════════════════════════════════

async def record_pause_with_reason(
    session: AsyncSession,
    sla: CaseSLAInstanceModel,
    reason: str,
    actor_id: Optional[str] = None,
) -> None:
    log_entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "actor_id": actor_id,
        "type": "pause",
    }
    logs = list(sla.pause_reasons_log or [])
    logs.append(log_entry)
    sla.pause_reasons_log = logs
    sla.pause_reason = reason
    await session.flush()


async def record_resume(
    session: AsyncSession,
    sla: CaseSLAInstanceModel,
    actor_id: Optional[str] = None,
) -> None:
    log_entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "actor_id": actor_id,
        "type": "resume",
        "previous_reason": sla.pause_reason,
    }
    logs = list(sla.pause_reasons_log or [])
    logs.append(log_entry)
    sla.pause_reasons_log = logs
    sla.pause_reason = None
    await session.flush()



async def resolve_escalation_tree_for_policy(
    session: AsyncSession,
    case_type_id: Optional[uuid.UUID],
    sla_policy: dict,
    tenant_id: Optional[str] = None,
) -> Optional[EscalationTreeModel]:
    """Resolve the escalation tree for a given SLA policy, with stage override.

    Priority:
      1. Explicit escalation_tree_id on the SLA policy JSON
      2. Case-type default tree (via resolve_escalation_tree)
      3. Global tree

    The SLA policy definition may include:
        {
          "id": "...",
          "goal_duration": "...",
          "deadline_duration": "...",
          "escalation_tree_id": "<uuid>"   # P34b — explicit override
        }

    If escalation_tree_id is set but the tree is missing or inactive,
    falls back to case-type/global resolution (does NOT fail silently — warns).
    """
    tree_id = (sla_policy or {}).get("escalation_tree_id")
    if tree_id:
        try:
            tid = uuid.UUID(str(tree_id))
        except (ValueError, TypeError):
            log.warning("invalid escalation_tree_id on SLA policy: %r", tree_id)
            tid = None
        if tid is not None:
            t = await session.get(EscalationTreeModel, tid)
            if t is not None and t.is_active:
                return t
            log.warning(
                "SLA policy references tree %s but it is missing/inactive — "
                "falling back to case-type default",
                tree_id,
            )
    return await resolve_escalation_tree(session, case_type_id, tenant_id)
