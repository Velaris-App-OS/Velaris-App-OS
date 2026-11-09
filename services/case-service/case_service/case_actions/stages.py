"""Stage-level status and actions — the second half of the 198 model.

Design: `docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md` (§7, stage-level control).

**Why a stage needs an opinion at all.** Migration 198 made a status data an
operator authors and an action a closed vocabulary in code, and it put both on
the TENANT and the CASE TYPE. What it left out is the thing a case designer
actually reaches for: *while a case is sitting in this stage*, what is it, and
what may be done to it. Today the only way to say "entering Review means the
case is under_review" is a rule writing `velaris.status` — a rule engine
invocation to express a property of the stage — and there is no way at all to
say "you cannot close a case that is still in Intake".

Two keys on the stage, both **absent by default**:

* ``on_enter_status`` — entering this stage moves the case to this status.
* ``actions_allowed`` — the actions offered while the case sits here.

**Both NARROW, and only narrow.** An action must pass the *status* rule
(``case_action_config.allowed_from``) **and** the *stage* rule. Two stages can
share a status, so a stage rule can never stand in for the status rule, and a
rule that could WIDEN is exactly the hole found and closed on 2026-09-07, when a
tenant could rebind `close` to a privilege every viewer holds. Deny-overrides
survives here for the same reason it survives in F-12's matcher: the wider
answer is never produced.

**Absent is not empty.** A stage with no ``actions_allowed`` key offers every
action, which is today's behaviour on every install. A stage with an EMPTY list
offers none — a designer who typed a list and then cleared it meant something,
and reading that as "all of them" would be the same `None`-versus-`[]` mistake
that #29's manifest permissions met in August.

**`on_enter_status` never breaks a stage move.** The stage transition is the
operator's act; the status is a consequence the designer configured. If the
configured status is unknown, disabled or unreachable from where the case is,
the case still moves and the refusal is written to the audit log under
`stage_status_not_applied` with the reason. Failing the transition instead would
strand a case on a designer's typo — and the operator who is blocked is not the
person who can fix it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db import repository as repo

from case_service.case_actions import registry, statuses

log = logging.getLogger(__name__)

#: The two keys this module owns, so a validator and a UI cannot drift from it.
KEY_ON_ENTER_STATUS = "on_enter_status"
KEY_ACTIONS_ALLOWED = "actions_allowed"


# ── reading the definition ───────────────────────────────────────────────────

def on_enter_status(stage: dict[str, Any] | None) -> str | None:
    """The status entering this stage sets, or None when the stage says nothing."""
    if not stage:
        return None
    raw = stage.get(KEY_ON_ENTER_STATUS)
    if raw is None:
        return None
    name = str(raw).strip()
    return name or None


def allowed_actions(stage: dict[str, Any] | None) -> tuple[str, ...] | None:
    """The actions this stage offers.

    **`None` means "the stage does not restrict actions"** — every install today,
    and every stage nobody has configured. An empty tuple means "this stage
    offers none", which is a thing a designer can deliberately say (a stage a
    case only passes through under workflow control). The two are not the same
    answer and must not collapse into one.
    """
    if not stage or KEY_ACTIONS_ALLOWED not in stage:
        return None
    raw = stage.get(KEY_ACTIONS_ALLOWED)
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        # A malformed value is not a licence to ignore the field: a designer
        # meant to narrow, and reading a string as "no restriction" would be the
        # permissive reading of a control whose whole purpose is to restrict.
        # It is also refused at authoring time, so this is belt and braces.
        log.warning("stage %r has a non-list %s; treating it as no actions",
                    (stage or {}).get("id"), KEY_ACTIONS_ALLOWED)
        return ()
    return tuple(str(x).strip() for x in raw if str(x).strip())


async def stage_def(
    session: AsyncSession,
    *,
    case_type_id: uuid.UUID | None,
    stage_id: str | None,
) -> dict[str, Any] | None:
    """The stage's definition out of the case type, or None.

    Reads `case_types.definition_json["stages"]`, which is the same list
    `transition_stage` and `case_lifecycle` already read — deliberately, so
    there is one authored shape and not a second table to keep in step.
    """
    if not case_type_id or not stage_id:
        return None
    try:
        ct = await repo.get_case_type(session, case_type_id)
    except Exception as exc:                                    # noqa: BLE001
        # Unreadable configuration means "as before", never "nothing is
        # permitted" — the posture `statuses.resolve_set` takes one module over.
        log.warning("stage config unreadable for case type %s: %s", case_type_id, exc)
        return None
    ct_def = (ct.definition_json or {}) if ct else {}
    for s in ct_def.get("stages", []) or []:
        if isinstance(s, dict) and str(s.get("id")) == str(stage_id):
            return s
    return None


# ── the gate ─────────────────────────────────────────────────────────────────

def action_offered(stage: dict[str, Any] | None, action_name: str) -> bool:
    """Does this stage offer this action? True when the stage says nothing."""
    allowed = allowed_actions(stage)
    if allowed is None:
        return True
    return action_name in allowed


# ── the effect ───────────────────────────────────────────────────────────────

async def apply_on_enter_status(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    stage: dict[str, Any] | None,
    actor: str | None,
    tenant_uuid: uuid.UUID | None = None,
) -> str | None:
    """Move the case to the stage's `on_enter_status`, if it has one.

    Returns the status actually applied, or None when nothing was done. **Never
    raises** — see the module docstring: the stage move has already happened and
    a configuration error must not strand the case.

    The write goes through `apply_status_change`, the same function every other
    status change goes through, so the audit row, the SLA lifecycle hook, the
    realtime broadcast and the outbox webhook all happen. Writing the column
    here would silently drop four of them, which is the mistake the dispatcher's
    own status handler documents.
    """
    target = on_enter_status(stage)
    if not target:
        return None

    case = await repo.get_case_instance(session, case_id)
    if case is None:                                            # pragma: no cover
        return None
    if (case.status or "") == target:
        # Already there. Not an error and not worth an audit row — a case
        # re-entering a stage would otherwise log a status change that did not
        # happen, and a log full of non-events is how a real one gets missed.
        return None

    try:
        await statuses.validate_transition(
            session, current=case.status, target=target,
            tenant_id=tenant_uuid, case_type_id=case.case_type_id,
        )
    except statuses.StatusError as exc:
        log.warning("stage %s: on_enter_status %r not applied to case %s: %s",
                    (stage or {}).get("id"), target, case_id, exc)
        await _audit_not_applied(session, case_id, stage, target, str(exc), exc.code)
        return None
    except Exception as exc:                                    # noqa: BLE001
        log.warning("stage %s: status set unreadable for case %s: %s",
                    (stage or {}).get("id"), case_id, exc)
        return None

    from case_service.api.routers.cases import apply_status_change

    await apply_status_change(
        session, case_id, target, actor,
        reason=f"Entered stage '{(stage or {}).get('name') or (stage or {}).get('id')}'",
        audit_action="status_changed",
    )
    return target


#: The status a case moves to on its first real progress, when nothing else has
#: an opinion. `new` and `open` are the two "not started yet" statuses the
#: platform ships; a case on either has genuinely not been worked until now.
AUTO_PROGRESS_FROM = ("new", "open", "reopened")
AUTO_PROGRESS_TO = "in_progress"


async def apply_auto_in_progress(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    stage: dict[str, Any] | None,
    actor: str | None,
    tenant_uuid: uuid.UUID | None = None,
) -> str | None:
    """A case moves to `in_progress` when a step advances after it was opened.

    Settled with the user on 2026-09-08: `in_progress` earns its place beside
    `open` precisely because it is the one status nobody has to set by hand —
    `open` means "somebody may start", `in_progress` means "somebody did".

    **The stage's own `on_enter_status` wins, always.** If a stage says what the
    case becomes on entry, this does not fire — not even when the stage's status
    is the one this would have set. A designer who configured a status can see
    that configuration; a rule that quietly overrode it would be invisible from
    every screen, and the first symptom would be a case that keeps flipping back
    to a status nobody chose. So the automatic rule is the fallback, not the
    default, and it is expressed as "the stage said nothing" rather than as a
    precedence check on the resulting value.

    Only fires FROM the not-started statuses, so it never drags a case out of
    `on_hold`, out of a tenant's own waiting status, or back out of a terminal
    one. Never raises.
    """
    if on_enter_status(stage):
        # The stage owns the answer. Nothing to do, and deliberately no audit
        # row: not firing is the normal case, not an event.
        return None

    case = await repo.get_case_instance(session, case_id)
    if case is None:                                            # pragma: no cover
        return None
    if (case.status or "") not in AUTO_PROGRESS_FROM:
        return None

    try:
        await statuses.validate_transition(
            session, current=case.status, target=AUTO_PROGRESS_TO,
            tenant_id=tenant_uuid, case_type_id=case.case_type_id,
        )
    except statuses.StatusError as exc:
        # A tenant that removed `in_progress`, or narrowed what reaches it, has
        # said what it wants. Silently, because unlike `on_enter_status` nobody
        # configured this per stage — an audit row here would appear on every
        # step completion for the life of that install.
        log.debug("auto in_progress not applied to case %s: %s", case_id, exc)
        return None
    except Exception as exc:                                    # noqa: BLE001
        log.warning("auto in_progress: status set unreadable for %s: %s", case_id, exc)
        return None

    from case_service.api.routers.cases import apply_status_change

    await apply_status_change(
        session, case_id, AUTO_PROGRESS_TO, actor,
        reason="Work started on this case", audit_action="status_changed")
    return AUTO_PROGRESS_TO


async def _audit_not_applied(
    session: AsyncSession,
    case_id: uuid.UUID,
    stage: dict[str, Any] | None,
    target: str,
    reason: str,
    code: str,
) -> None:
    """A configured effect that did not happen is worth a row of its own.

    Silence here would be the worst outcome: a designer would see the stage move
    and assume the status followed it, and nothing anywhere would say otherwise.
    """
    try:
        from case_service.api.routers.cases import _audit

        await _audit(
            session, case_id, "stage_status_not_applied",
            actor_type="system",
            new_value={"status": target},
            details={"stage_id": (stage or {}).get("id"), "code": code,
                     "reason": reason},
        )
    except Exception as exc:                                    # noqa: BLE001
        log.warning("stage_status_not_applied not audited for %s: %s", case_id, exc)


# ── authoring-time validation ────────────────────────────────────────────────

class StageConfigError(ValueError):
    """A stage definition the platform will not store."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def validate_actions_allowed(stages: Iterable[Any]) -> None:
    """Refuse an `actions_allowed` naming something that is not an action.

    Checked at authoring rather than at runtime because the action vocabulary is
    **closed and lives in code**: a name that is not in the registry today can
    never become one by an operator's act, so the only thing runtime tolerance
    would buy is a stage that silently offers fewer actions than its author
    listed. That is the failure mode this whole feature is supposed to remove.
    """
    known = set(registry.ACTIONS)
    for stage in stages or []:
        if not isinstance(stage, dict):
            continue
        if KEY_ACTIONS_ALLOWED not in stage or stage.get(KEY_ACTIONS_ALLOWED) is None:
            continue
        raw = stage.get(KEY_ACTIONS_ALLOWED)
        if not isinstance(raw, (list, tuple)):
            raise StageConfigError(
                f"Stage '{stage.get('id')}': {KEY_ACTIONS_ALLOWED} must be a list "
                f"of action names.",
                code="VEL-CASE-STAGE-CONFIG-400")
        unknown = sorted({str(x) for x in raw} - known)
        if unknown:
            raise StageConfigError(
                f"Stage '{stage.get('id')}' offers {', '.join(unknown)}, which "
                f"{'are' if len(unknown) > 1 else 'is'} not "
                f"{'actions' if len(unknown) > 1 else 'an action'}. The action "
                f"vocabulary is fixed: {', '.join(sorted(known))}.",
                code="VEL-CASE-STAGE-CONFIG-400")


async def validate_on_enter_status(
    session: AsyncSession,
    stages: Iterable[Any],
    *,
    tenant_id: uuid.UUID | None,
    case_type_id: uuid.UUID | None,
) -> None:
    """Refuse an `on_enter_status` naming a status that does not exist here.

    Resolved at the scope the case type will run in, so a tenant's own status is
    accepted and a neighbouring tenant's is not. A status that cannot be read at
    all falls back to the pre-198 set rather than refusing the deploy.
    """
    wanted = {
        s.get(KEY_ON_ENTER_STATUS)
        for s in (stages or [])
        if isinstance(s, dict) and s.get(KEY_ON_ENTER_STATUS)
    }
    if not wanted:
        return
    resolved = await statuses.resolve_set(
        session, tenant_id=tenant_id, case_type_id=case_type_id)
    known = {name for name, s in resolved.items() if s.enabled}
    unknown = sorted({str(w) for w in wanted} - known)
    if unknown:
        raise StageConfigError(
            f"No status named {', '.join(unknown)} in this scope. Create the "
            f"status first — a stage that sets a status nobody defined would "
            f"move the case and change nothing. Known: "
            f"{', '.join(sorted(known))}.",
            code="VEL-CASE-STAGE-CONFIG-400")
