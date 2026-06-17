"""Assignment router: evaluates AssignmentRules to determine targets.

Given an ``AssignmentRule`` from the case type IR, this module
resolves the concrete assignee (user, role, or queue) and creates
the assignment record.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db import repository as repo
from case_service.db.models import CaseAssignmentModel

logger = logging.getLogger(__name__)


# ─── Strategy resolvers ───────────────────────────────────────────


async def _resolve_specific_user(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Return (assignee_type, assignee_id) for SPECIFIC_USER."""
    return "user", rule.get("target", "unassigned")


async def _resolve_role_based(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Assign to a role — any member of that role can claim."""
    return "role", rule.get("target", "default-role")


async def _resolve_queue_based(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Place the item on a named queue for self-service pickup."""
    return "queue", rule.get("target", "default-queue")


async def _resolve_round_robin(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Assign to the user in the role who was assigned longest ago.

    Looks at the most recent assignment per user in the target role
    and picks the one with the oldest ``assigned_at``.
    """
    role = rule.get("target", "default-role")
    # Find all active assignments for this role, grouped by assignee,
    # and pick the one whose latest assignment is oldest.
    stmt = (
        select(
            CaseAssignmentModel.assignee_id,
            func.max(CaseAssignmentModel.assigned_at).label("last_assigned"),
        )
        .where(
            CaseAssignmentModel.assignee_type == "user",
            CaseAssignmentModel.status == "active",
        )
        .group_by(CaseAssignmentModel.assignee_id)
        .order_by(func.max(CaseAssignmentModel.assigned_at).asc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row:
        return "user", row.assignee_id

    # Fallback: nobody has been assigned yet → assign to role
    return "role", role


async def _resolve_least_loaded(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Assign to the user with the fewest active assignments."""
    stmt = (
        select(
            CaseAssignmentModel.assignee_id,
            func.count().label("load"),
        )
        .where(
            CaseAssignmentModel.assignee_type == "user",
            CaseAssignmentModel.status == "active",
        )
        .group_by(CaseAssignmentModel.assignee_id)
        .order_by(func.count().asc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row:
        return "user", row.assignee_id

    role = rule.get("target", "default-role")
    return "role", role


async def _resolve_self_service(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Place on queue — user must explicitly claim."""
    return "queue", rule.get("target", "default-queue")


async def _resolve_rule_based(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Evaluate a stored business rule to determine the assignment target.

    The rule's definition_json must produce one of:
      - A ``when``/``routing`` rule with an action:
          {"action_type": "assign_to", "target": "<user|role|queue>", "value": "<id>"}
      - A ``decision_table`` rule with outcomes:
          {"assignee_type": "<user|role|queue>", "assignee_id": "<id>"}
    Falls back to queue on any error or non-match.
    """
    import uuid as _uuid
    from case_service.core.rules_evaluator import evaluate_rule as _eval_rule

    rule_id = rule.get("rule_id", "")
    if not rule_id:
        logger.warning("rule_based strategy missing rule_id, falling back to queue")
        return "queue", rule.get("target", "default-queue")

    try:
        db_rule = await repo.get_rule(session, _uuid.UUID(str(rule_id)))
    except (ValueError, Exception) as exc:
        logger.warning("Invalid rule_id %s: %s", rule_id, exc)
        return "queue", rule.get("target", "default-queue")

    if db_rule is None or not db_rule.enabled:
        logger.warning("Business rule %s not found or disabled", rule_id)
        return "queue", rule.get("target", "default-queue")

    rule_dict = {
        "id": str(db_rule.id),
        "name": db_rule.name,
        "rule_type": db_rule.rule_type,
        "priority": db_rule.priority,
        **db_rule.definition_json,
    }
    result = _eval_rule(rule_dict, context)

    # decision_table: outcomes dict
    outcomes = result.get("outcomes", {})
    if "assignee_type" in outcomes and "assignee_id" in outcomes:
        return outcomes["assignee_type"], outcomes["assignee_id"]

    # when/routing: assign_to action
    for action in result.get("action_results") or []:
        if action.get("action") == "assign_to":
            return action.get("target", "queue"), action.get("value", rule.get("target", "default-queue"))

    if result.get("matched"):
        logger.info("Business rule %s matched but produced no assignment action", rule_id)
    else:
        logger.info("Business rule %s did not match context, using fallback", rule_id)

    return "queue", rule.get("target", "default-queue")


async def _resolve_manager_of(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Assign to the manager of the current assignee.

    TODO: Look up org hierarchy via user-service protocol.
    """
    current_assignee = context.get("current_assignee_id", "")
    logger.info("Manager-of routing for %s (stub)", current_assignee)
    return "role", "manager"


async def _resolve_skill_based(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Find a user matching required skills.

    TODO: Query user-service for skill matching.
    """
    skills = rule.get("skill_requirements", [])
    logger.info("Skill-based routing for skills %s (stub)", skills)
    return "queue", rule.get("target", "default-queue")


_RESOLVERS = {
    "specific_user": _resolve_specific_user,
    "role_based": _resolve_role_based,
    "queue_based": _resolve_queue_based,
    "round_robin": _resolve_round_robin,
    "least_loaded": _resolve_least_loaded,
    "self_service": _resolve_self_service,
    "rule_based": _resolve_rule_based,
    "manager_of": _resolve_manager_of,
    "skill_based": _resolve_skill_based,
}


# ─── Public API ───────────────────────────────────────────────────


async def resolve_assignment(
    session: AsyncSession,
    rule: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Resolve an ``AssignmentRule`` dict to (assignee_type, assignee_id).

    If *rule* is ``None`` or the strategy is unknown, falls back to
    queue-based assignment.  If the primary strategy fails to find
    a target and a ``fallback_strategy`` is set, tries that next.
    """
    if rule is None:
        return "queue", "default-queue"

    ctx = context or {}
    strategy = rule.get("strategy", "queue_based")
    resolver = _RESOLVERS.get(strategy, _resolve_queue_based)

    assignee_type, assignee_id = await resolver(session, rule, ctx)

    # If resolver returned a sentinel "unassigned" and a fallback exists
    if assignee_id == "unassigned" and rule.get("fallback_strategy"):
        fb_strategy = rule["fallback_strategy"]
        fb_resolver = _RESOLVERS.get(fb_strategy, _resolve_queue_based)
        fb_rule = {**rule, "target": rule.get("fallback_target")}
        assignee_type, assignee_id = await fb_resolver(session, fb_rule, ctx)

    return assignee_type, assignee_id


async def create_assignment_for_step(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    step: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> CaseAssignmentModel:
    """Create a work-item assignment for a single step.

    Evaluates the step's ``assignment`` rule to determine the target,
    then persists the assignment record.
    """
    assignment_rule = step.get("assignment")

    # #22 DecisionPoint: policy is the default path (identical behavior);
    # the AI resolver participates only when the case type opts in AND the
    # step's cognitive mode is "automatic". AI failure → policy, always.
    from case_service.decisions import cognitive_mode_for_step, decision_ai_config
    from case_service.decisions.assignment import assignment_decision_point
    ai_enabled, threshold = False, 1.0
    mode = cognitive_mode_for_step(step)
    if mode == "automatic":
        from sqlalchemy import select as _select
        from case_service.db.models import CaseInstanceModel, CaseTypeModel
        ct_def = (await session.execute(
            _select(CaseTypeModel.definition_json)
            .join(CaseInstanceModel, CaseInstanceModel.case_type_id == CaseTypeModel.id)
            .where(CaseInstanceModel.id == case_id)
        )).scalar_one_or_none()
        ai_enabled, threshold = decision_ai_config(ct_def)

    outcome = await assignment_decision_point.resolve(
        session,
        {"rule": assignment_rule, "context": context},
        case_id=case_id,
        ai_enabled=ai_enabled,
        threshold=threshold,
        cognitive_mode=mode,
    )
    assignee_type = outcome.decision["assignee_type"]
    assignee_id = outcome.decision["assignee_id"]

    # Compute due date from step SLA if available
    due_at = None
    sla_policy_id = step.get("sla_policy_id")
    if sla_policy_id:
        # TODO: look up SLA policy duration and compute due_at
        pass

    assignment = await repo.create_assignment(
        session,
        data={
            "case_id": case_id,
            "step_id": step["id"],
            "assignee_type": assignee_type,
            "assignee_id": assignee_id,
            "due_at": due_at,
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "assignment_created",
            "actor_type": "system",
            "details": {
                "step_id": step["id"],
                "assignee_type": assignee_type,
                "assignee_id": assignee_id,
                "strategy": (assignment_rule or {}).get("strategy", "default"),
            },
        },
    )

    return assignment


async def create_assignments_for_stage(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    stage_id: str,
    steps: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> list[CaseAssignmentModel]:
    """Create assignments for all steps in a stage."""
    assignments = []
    for step in steps:
        a = await create_assignment_for_step(
            session, case_id=case_id, step=step, context=context
        )
        assignments.append(a)

    logger.info(
        "Created %d assignments for case %s stage %s",
        len(assignments),
        case_id,
        stage_id,
    )
    return assignments
