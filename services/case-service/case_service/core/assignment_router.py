"""Assignment router: evaluates AssignmentRules to determine targets.

Given an ``AssignmentRule`` from the case type IR, this module
resolves the concrete assignee (user, role, or queue) and creates
the assignment record.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db import repository as repo
from velaris_shared.db.models import CaseAssignmentModel, UserDirectoryModel

logger = logging.getLogger(__name__)

#: How many candidates a load or turn-order query considers before the
#: availability filter runs. Bounded because both queries are unbounded scans of
#: the assignment table; deep enough that a whole team being away is what it
#: takes to fall through to the role.
_CANDIDATE_LIMIT = 50


# ─── Availability (Calendar) ────────────────────────────────────
#
# Until Calendar existed, every strategy below picked people who were on
# holiday: `business_calendars` describes the organisation, and nothing
# described a person. These three helpers are the whole integration, and each
# one **fails open** — an unanswerable calendar must never stop work being
# assigned, because the failure mode of guessing "absent" is a queue that
# silently fills up while everybody looks idle.


async def _tenant_for(session: AsyncSession, context: dict[str, Any]) -> str:
    """The tenant whose calendar applies, from the context or from the case."""
    tenant = context.get("tenant_id")
    if tenant:
        return str(tenant)
    case_id = context.get("case_id")
    if case_id:
        from velaris_shared.db.models import CaseInstanceModel

        row = await session.get(CaseInstanceModel, case_id)
        if row is not None and getattr(row, "tenant_id", None):
            return str(row.tenant_id)
    return "default"


async def _available_only(
    session: AsyncSession, user_ids: list[str], context: dict[str, Any],
) -> list[str]:
    """The candidates who are not on approved leave, in the order given.

    **Leave only — never working hours.** An absence is an explicit fact
    somebody approved; a working pattern is an assumption, and the platform
    default (09:00–17:00, Mon–Fri) is one nobody chose. Filtering on hours here
    would mean a case arriving at 22:00 is assigned to nobody, and every 24/7
    deployment that never configured patterns would silently queue everything
    overnight. Who *owns* the work is not the same question as who is at their
    desk this minute.
    """
    if not user_ids:
        return []
    try:
        from case_service.calendar import service as calendar

        return await calendar.not_absent(
            session, user_ids, tenant_id=await _tenant_for(session, context))
    except Exception:                                    # pragma: no cover
        logger.warning("availability filter unavailable; assigning as before",
                       exc_info=True)
        return list(user_ids)


async def _cover_for(
    session: AsyncSession, user_id: str, context: dict[str, Any],
) -> str | None:
    """Who covers for a named person who is away — `None` if nobody does.

    Only used where the rule named **one** person. A named target is a decision
    somebody made, so it is honoured unless that person is away AND has
    nominated cover; silently spreading it across a team would be worse than a
    late reply.
    """
    try:
        from case_service.calendar import service as calendar

        tenant = await _tenant_for(session, context)
        if not await calendar.absent_at(session, user_id, tenant_id=tenant):
            return None
        return await calendar.delegate_for(session, user_id, tenant_id=tenant)
    except Exception:                                    # pragma: no cover
        logger.warning("cover lookup failed for %s", user_id, exc_info=True)
        return None


# ─── Strategy resolvers ───────────────────────────────────────────


async def _resolve_specific_user(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Return (assignee_type, assignee_id) for SPECIFIC_USER.

    If that person is away and has nominated cover, the cover gets it. With no
    cover the assignment stands: a named target is somebody's decision, and
    rerouting it to a queue would lose the reason it was named.
    """
    target = rule.get("target", "unassigned")
    if target and target != "unassigned":
        # A named person who has been DELETED is not "somebody's decision" any
        # more: new work would sit on an account nobody can sign in to. It goes
        # to the rule's fallback (or the default queue), and the rule is flagged
        # for an administrator to fix (Nov_TODO "User deletion v2", 8).
        if await _target_deleted(session, str(target)):
            logger.warning("specific_user: %s was deleted — routing to the fallback", target)
            await _flag_dead_target(session, str(target), context)
            if rule.get("fallback_strategy"):
                return "user", "unassigned"            # resolve_assignment runs the fallback
            return "queue", rule.get("fallback_target") or "default-queue"
        cover = await _cover_for(session, str(target), context)
        if cover and await _target_deleted(session, str(cover)):
            cover = None                               # a deleted cover covers nothing
        if cover:
            logger.info("specific_user: %s is away, assigning cover %s", target, cover)
            return "user", cover
    return "user", target


async def _target_deleted(session: AsyncSession, target: str) -> bool:
    """The named account is deleted (in its grace period) or purged (the
    deletion ledger names it). An unknown name that was never an account is
    not "deleted" — it is routed as before."""
    from sqlalchemy import or_, select as _select

    from velaris_shared.db.models import HelixUserModel
    from velaris_shared.identity import ledger
    try:
        tid = uuid.UUID(target)
    except ValueError:
        tid = None
    conds = [HelixUserModel.username == target]
    if tid is not None:
        conds.append(HelixUserModel.id == tid)
    row = (await session.execute(_select(HelixUserModel.deleted_at).where(or_(*conds)))).first()
    if row is not None:
        return row[0] is not None
    if target in await ledger.purged_ids(session):
        return True
    return ledger.name_hash(target) in await ledger.purged_name_hashes(session)


async def _flag_dead_target(session: AsyncSession, target: str, context: dict[str, Any]) -> None:
    try:
        from velaris_shared.db.security_events import log_security_event
        await log_security_event(
            session, event_type="routing_target_deleted", severity="medium",
            resource_type="assignment_rule", resource_id=str(context.get("case_type_id") or ""),
            action="route", outcome="fallback",
            details={"target": target, "case_id": str(context.get("case_id") or ""),
                     "fix": "this rule names a deleted user — change its target"})
    except Exception:  # noqa: BLE001 — never fail an assignment over a flag
        logger.warning("could not flag a rule naming deleted user %s", target, exc_info=True)


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
    # Every candidate in turn-order rather than only the first: the person whose
    # turn it is may be on holiday, and the point of round robin is the next
    # person, not an empty desk.
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
        .limit(_CANDIDATE_LIMIT)
    )
    candidates = [r.assignee_id for r in (await session.execute(stmt)).all()]
    if candidates:
        free = await _available_only(session, candidates, context)
        if free:
            return "user", free[0]
        logger.info("round_robin: everybody in turn-order is away, using role %s", role)

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
        .limit(_CANDIDATE_LIMIT)
    )
    # Lightest first, then the lightest who is actually in. Somebody on holiday
    # has the lightest load of all, which is exactly why this needs the filter.
    candidates = [r.assignee_id for r in (await session.execute(stmt)).all()]
    role = rule.get("target", "default-role")
    if candidates:
        free = await _available_only(session, candidates, context)
        if free:
            return "user", free[0]
        logger.info("least_loaded: every candidate is away, using role %s", role)

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

    # 8e: the case type names a rule by id; a tenant that forked that default
    # rule gets its fork. The case's own tenant decides — not the caller's.
    if db_rule is not None and context.get("case_id"):
        from velaris_shared.db.models import CaseInstanceModel, RuleDefinitionModel
        from velaris_shared.tenancy import inherit
        case_tenant = await session.scalar(
            select(CaseInstanceModel.tenant_id).where(
                CaseInstanceModel.id == _uuid.UUID(str(context["case_id"]))))
        db_rule = await inherit.resolve(session, RuleDefinitionModel, db_rule, case_tenant)

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

    The org hierarchy is `user_directory.manager_user_id`. Every step degrades
    to the `manager` role rather than failing, because a case that cannot find
    a named manager still has to go somewhere: no current assignee, no
    directory row, no manager recorded, or a manager who has been deactivated.
    """
    current_assignee = str(context.get("current_assignee_id") or "").strip()
    if not current_assignee:
        logger.info("manager_of: no current assignee, falling back to manager role")
        return "role", "manager"

    stmt = select(UserDirectoryModel.manager_user_id).where(
        UserDirectoryModel.user_id == current_assignee,
        UserDirectoryModel.is_active.is_(True),
    )
    manager_id = (await session.execute(stmt)).scalar_one_or_none()
    if not manager_id:
        logger.info("manager_of: no manager recorded for %s", current_assignee)
        return "role", "manager"

    # The manager must still be an active user — a stale pointer to a leaver
    # would silently park work on nobody.
    active = (
        await session.execute(
            select(UserDirectoryModel.user_id).where(
                UserDirectoryModel.user_id == manager_id,
                UserDirectoryModel.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not active:
        logger.info("manager_of: manager %s is inactive", manager_id)
        return "role", "manager"

    # A manager on leave who nominated cover hands over to them; with no cover
    # the escalation still reaches the manager, whose inbox is where it belongs.
    cover = await _cover_for(session, str(manager_id), context)
    if cover:
        logger.info("manager_of: %s is away, assigning cover %s", manager_id, cover)
        return "user", cover

    return "user", str(manager_id)


def _required_skills(rule: dict[str, Any]) -> list[str]:
    """Skill requirements, normalised to lowercase non-empty strings."""
    raw = rule.get("skill_requirements") or []
    if isinstance(raw, str):
        raw = [raw]
    return [s.strip().lower() for s in raw if isinstance(s, str) and s.strip()]


async def _resolve_skill_based(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """Assign to the least-loaded active user holding ALL required skills.

    Skills live in `user_directory.skills` (a JSON array of names) and are
    matched case-insensitively. ALL requirements must be held — a partial match
    is not a match, since "claims + fraud" asks for someone who can do both.

    Falls back to the rule's target queue whenever no one qualifies, which is
    also what an empty requirement list does: an unconfigured rule must not
    quietly resolve to an arbitrary person.
    """
    required = _required_skills(rule)
    fallback = rule.get("target", "default-queue")
    if not required:
        logger.info("skill_based: no skill_requirements set, using queue %s", fallback)
        return "queue", fallback

    rows = (
        await session.execute(
            select(UserDirectoryModel.user_id, UserDirectoryModel.skills).where(
                UserDirectoryModel.is_active.is_(True)
            )
        )
    ).all()

    # Matching in Python, not SQL: the JSON containment operators differ across
    # PostgreSQL, MySQL and MariaDB, and the directory is small enough that a
    # portable scan is worth more than a dialect-specific query here.
    needed = set(required)
    qualified = [
        r.user_id
        for r in rows
        if needed <= {s.strip().lower() for s in (r.skills or []) if isinstance(s, str)}
    ]

    if not qualified:
        logger.info("skill_based: nobody holds %s, using queue %s", required, fallback)
        return "queue", fallback

    # Holding the skill is not the same as being at work today.
    present = await _available_only(session, qualified, context)
    if not present:
        logger.info("skill_based: everybody holding %s is away, using queue %s",
                    required, fallback)
        return "queue", fallback
    qualified = present

    # Spread the work: pick whoever of the qualified holds the fewest active
    # assignments. Anyone with zero assignments has no row here, so they win by
    # being absent from the load map.
    load_rows = (
        await session.execute(
            select(CaseAssignmentModel.assignee_id, func.count().label("load"))
            .where(
                CaseAssignmentModel.assignee_type == "user",
                CaseAssignmentModel.status == "active",
                CaseAssignmentModel.assignee_id.in_(qualified),
            )
            .group_by(CaseAssignmentModel.assignee_id)
        )
    ).all()
    load = {r.assignee_id: r.load for r in load_rows}

    chosen = min(sorted(qualified), key=lambda uid: load.get(uid, 0))
    return "user", str(chosen)


async def _resolve_workforce(
    session: AsyncSession, rule: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str]:
    """F-05: assign to an AGENT if one qualifies, otherwise to a human.

    A NEW strategy rather than a change to `skill_based`, and that is the whole
    upgrade story: every existing case type keeps the strategy it names, so no
    install starts routing work to agents because it took an update. A case type
    opts in by naming `workforce`.

    Agents first, then the human path — deliberately in that order. The point of
    the feature is that an agent takes the step when it can; a human is the
    fallback, and the fallback is the ordinary skill-based resolution, so
    nothing about human routing is reimplemented here.

    `assignable_to` on the rule mirrors the design's per-step setting:
    `human` skips agents entirely, `agent` refuses to fall back to a person,
    and `either` (the default) prefers an agent and falls back.
    """
    from case_service.agents import workforce

    assignable = str(rule.get("assignable_to", "either")).lower()
    if assignable not in ("human", "agent", "either"):
        # An unrecognised value routes to humans. Fail towards the party that
        # can ask questions.
        logger.warning("workforce: unknown assignable_to %r, routing to humans",
                       assignable)
        assignable = "human"

    if assignable != "human":
        tenant = await _tenant_for(session, context)
        try:
            ranked = await workforce.eligible(
                session, tenant_id=tenant,
                required_skills=_required_skills(rule))
        except Exception:                                      # noqa: BLE001
            # A broken roster must not stop work being assigned at all.
            logger.warning("workforce: agent selection failed, using the human "
                           "path", exc_info=True)
            ranked = []
        if ranked:
            chosen, _score = ranked[0]
            return "agent", str(chosen.agent_id)
        if assignable == "agent":
            # Agent-only work with no available agent goes to the ESCALATION
            # queue, never to nobody. `target` is the rule's own fallback queue.
            logger.info("workforce: no agent available for an agent-only step, "
                        "using queue %s", rule.get("target", "default-queue"))
            return "queue", rule.get("target", "default-queue")

    return await _resolve_skill_based(session, rule, context)


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
    # F-05. Opt-in per rule: an install that names none of its steps
    # `workforce` never routes anything to an agent.
    "workforce": _resolve_workforce,
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


async def _due_at_for_step(
    session: AsyncSession, *, case_id: uuid.UUID, step: dict[str, Any]
) -> datetime | None:
    """When this work item is due, from the step's SLA policy.

    Uses the policy's **goal** duration, not its deadline: the deadline is the
    breach point, while the goal is the "you should be done by" target. The
    Work Center renders a past `due_at` as *Overdue*, so pinning it to the goal
    warns the assignee while there is still time to act. Falls back to the
    deadline when a policy declares no goal.

    Returns None whenever the due date cannot be established — no SLA on the
    step, an unresolvable case type, a policy id the definition does not carry,
    or an unparseable duration. A missing due date is the correct outcome
    there; inventing one would make the Work Center lie.
    """
    sla_policy_id = step.get("sla_policy_id")
    if not sla_policy_id:
        return None

    from case_service.core.sla_tracker import parse_iso8601_duration
    from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel

    ct_def = (
        await session.execute(
            select(CaseTypeModel.definition_json)
            .join(CaseInstanceModel, CaseInstanceModel.case_type_id == CaseTypeModel.id)
            .where(CaseInstanceModel.id == case_id)
        )
    ).scalar_one_or_none()
    if not ct_def:
        return None

    for policy in (ct_def.get("sla_policies") or []):
        if policy.get("id") != sla_policy_id:
            continue
        duration = policy.get("goal_duration") or policy.get("deadline_duration")
        if not duration:
            return None
        try:
            return datetime.now(timezone.utc) + parse_iso8601_duration(duration)
        except ValueError:
            logger.warning(
                "SLA policy %s has an unparseable duration %r; assignment left undated",
                sla_policy_id, duration,
            )
            return None

    logger.warning(
        "Step %s references SLA policy %s not present in the case type definition",
        step.get("id"), sla_policy_id,
    )
    return None


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
        from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
        ct_def = (await session.execute(
            _select(CaseTypeModel.definition_json)
            .join(CaseInstanceModel, CaseInstanceModel.case_type_id == CaseTypeModel.id)
            .where(CaseInstanceModel.id == case_id)
        )).scalar_one_or_none()
        ai_enabled, threshold = decision_ai_config(ct_def)

    # The calendar needs to know whose calendar to read, and the strategies
    # reach it only through the context.
    context = dict(context or {})
    context.setdefault("case_id", case_id)

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
    due_at = await _due_at_for_step(session, case_id=case_id, step=step)

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

    # #30a: assignment is one of the five events §2.2 names for the Case
    # aggregate. Recorded here rather than at the router so every path that
    # creates an assignment is covered, not just the HTTP one.
    from case_service.core import event_store as _es
    await _es.record(
        session, case_id=case_id, event_type=_es.ASSIGNED,
        data={
            "step_id": step["id"],
            "assignee_type": assignee_type,
            "assignee_id": assignee_id,
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
