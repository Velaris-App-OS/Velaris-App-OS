"""Case statuses — resolved from data, not from a tuple in a module.

Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md

**Why this module exists.** `case_instances.status` is a `String(30)` with no
validation anywhere: `POST /cases/{id}/status` accepted any string a caller
sent. Six statuses existed on this platform because six were used, not because
six were designed. And *which of them ends a case* was a hardcoded tuple in two
places — `temporal/workflows/case_lifecycle_workflow.py` and
`temporal/activities/interpreter_activities.py` — so a new terminal status was
correct in the database and wrong in the workflow engine, with nothing saying so.

**Compatibility, which is the whole reason this is safe.** Migration 198 seeds
the platform set with `allowed_from = []` on every row, and an empty
`allowed_from` means *from any status*. So the transition graph starts fully
permissive and every call that works today keeps working; tightening it is an
operator's decision, taken later, per case type. And an **undefined** status —
one a case is already sitting on that no row defines — is never refused on read:
validation applies to writes only, because refusing a stored value would make
that case unreadable rather than making it correct.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import CaseStatusModel

log = logging.getLogger(__name__)


#: What the platform falls back to when the table is unreachable or empty.
#:
#: This is not a convenience. A status set that cannot be loaded must not mean
#: "nobody may change a status" — that would take the platform down on a
#: transient database error — and it must not mean "any string is fine" either,
#: because that is the defect this module exists to close. It means **exactly
#: what the code did before migration 198**: the six statuses that were in use,
#: with the same three terminal, and no transition restrictions.
_FALLBACK: tuple[tuple[str, str, str, bool, bool], ...] = (
    # name, label, category, is_terminal, is_default
    ("new",       "New",       "open",     False, True),
    ("open",      "Open",      "open",     False, False),
    ("resolved",  "Resolved",  "terminal", True,  False),
    ("closed",    "Closed",    "terminal", True,  False),
    ("reopened",  "Reopened",  "open",     False, False),
    ("cancelled", "Cancelled", "terminal", True,  False),
    # These two are easy to miss and expensive to omit. They are live platform
    # statuses that no case on any box happens to be sitting on right now, so
    # they do not appear in a `SELECT DISTINCT status` — but `case_lifecycle`
    # PAUSES a case's SLAs on reaching either, `relationship_manager` moves a
    # parent off `pending_subcase` when its child resolves, and analytics
    # reports on both. Leaving them out of the fallback would make the sub-case
    # feature refuse its own transition the first time the set was unreadable.
    ("pending_external", "Pending External", "waiting", False, False),
    ("pending_subcase",  "Pending Subcase",  "waiting", False, False),
)


@dataclass(frozen=True)
class Status:
    name: str
    label: str
    category: str
    is_terminal: bool
    is_default: bool
    allowed_from: tuple[str, ...]
    sort_order: int
    enabled: bool

    def reachable_from(self, current: str | None) -> bool:
        """Empty `allowed_from` means from anywhere — the permissive default."""
        if not self.allowed_from:
            return True
        return (current or "") in self.allowed_from


class StatusError(ValueError):
    """A status write that the resolved set does not permit."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def scope_key(tenant_id: uuid.UUID | None, case_type_id: uuid.UUID | None) -> str:
    """`<tenant|platform>:<case_type|any>` — written, never generated.

    MariaDB 11.8 refuses `COALESCE` inside `GENERATED ALWAYS AS` (error 1901)
    where MySQL 8.4 accepts it, so the application writes this column on every
    dialect and a CHECK keeps it honest. Migration 177 met this first.
    """
    return f"{tenant_id or 'platform'}:{case_type_id or 'any'}"


def _to_status(row: CaseStatusModel) -> Status:
    raw = row.allowed_from or []
    return Status(
        name=row.name,
        label=row.label,
        category=row.category or "open",
        is_terminal=bool(row.is_terminal),
        is_default=bool(row.is_default),
        allowed_from=tuple(str(x) for x in raw if x),
        sort_order=int(row.sort_order or 0),
        enabled=bool(row.enabled),
    )


def _fallback_set() -> dict[str, Status]:
    return {
        name: Status(name=name, label=label, category=cat, is_terminal=term,
                     is_default=default, allowed_from=(), sort_order=i * 10,
                     enabled=True)
        for i, (name, label, cat, term, default) in enumerate(_FALLBACK)
    }


async def resolve_set(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    case_type_id: uuid.UUID | None = None,
) -> dict[str, Status]:
    """The status set in force for this scope, keyed by name.

    Most specific wins, and the layers **merge** rather than replace: a tenant
    that defines one extra status still gets the platform's ten. Replacing would
    make "add one status" mean "restate all eleven", and the eleventh would go
    missing the first time the platform set grew.
    """
    keys = [scope_key(None, None)]
    if tenant_id is not None:
        keys.append(scope_key(tenant_id, None))
    if tenant_id is not None and case_type_id is not None:
        keys.append(scope_key(tenant_id, case_type_id))
    if tenant_id is None and case_type_id is not None:
        keys.append(scope_key(None, case_type_id))

    try:
        rows = (await session.execute(
            select(CaseStatusModel).where(CaseStatusModel.scope_key.in_(keys))
        )).scalars().all()
    except Exception as exc:                                   # noqa: BLE001
        # A status set that cannot be loaded means "as before", never "nothing
        # is permitted" — the latter takes every status change on the platform
        # down on a transient error.
        log.warning("case status set unreadable, falling back to pre-198 set: %s", exc)
        return _fallback_set()

    if not rows:
        return _fallback_set()

    # Least specific first, so a more specific row overwrites it by name.
    order = {k: i for i, k in enumerate(keys)}
    out: dict[str, Status] = {}
    for row in sorted(rows, key=lambda r: order.get(r.scope_key, 0)):
        out[row.name] = _to_status(row)
    return out


async def is_terminal(
    session: AsyncSession,
    status: str | None,
    *,
    tenant_id: uuid.UUID | None = None,
    case_type_id: uuid.UUID | None = None,
) -> bool:
    """Does reaching this status end the case?

    Replaces `_TERMINAL_STATUSES` in the two Temporal modules. An **unknown**
    status is not terminal: a case sitting on a status nobody defined is still
    an open case, and treating it as finished would silently stop its workflow.
    """
    if not status:
        return False
    statuses = await resolve_set(session, tenant_id=tenant_id, case_type_id=case_type_id)
    found = statuses.get(status)
    return bool(found and found.is_terminal)


async def is_terminal_for_case(session: AsyncSession, case: Any) -> bool:
    """Does this case's CURRENT status end it, in this case's own scope?

    The case-shaped wrapper over `is_terminal`, and it exists because the
    hardcoded tuple this module replaced was never in only three places.
    Migration 198 found `("resolved", "closed", "cancelled")` in the two
    Temporal modules and in `core/case_lifecycle`; a sweep on 2026-09-08 found
    **eleven more**, most of them on the customer portal, where they decide
    whether a customer may still upload a document, complete a step, send a
    message or be shown the case as finished.

    That is the same defect one surface over: a tenant that adds its own
    terminal status — which this module exists to let them do — got a case that
    was closed everywhere except in front of the customer, who could go on
    working on it. Resolving per case rather than per platform is what makes the
    answer right for a tenant that defined the status and unchanged for one that
    did not.

    Falls back to the pre-198 set through `resolve_set`, so an unreadable status
    table means "as before" here too, never "nothing is finished".
    """
    if case is None:
        return False
    return await is_terminal(
        session, getattr(case, "status", None),
        tenant_id=getattr(case, "tenant_id", None),
        case_type_id=getattr(case, "case_type_id", None),
    )


async def _status_of(session: AsyncSession, case: Any) -> Status | None:
    if case is None:
        return None
    resolved = await resolve_set(
        session, tenant_id=getattr(case, "tenant_id", None),
        case_type_id=getattr(case, "case_type_id", None))
    return resolved.get(getattr(case, "status", None) or "")


async def is_workable_for_case(session: AsyncSession, case: Any) -> bool:
    """Is this case in a state where work may proceed on it?

    Not terminal, and not waiting. The fourth reading of "what state is this
    case in", and the one the auto-advance gate needs: a case that is finished
    must not advance a stage, and neither must one that is deliberately paused
    — `on_hold` and a tenant's own waiting statuses both mean *stop*, and
    advancing through a hold would make the hold a label rather than a control.

    Replaces a literal `["open", "new", "reopened"]` in `complete_step`, which
    had two problems at once: a tenant's own active status was not in it, and
    **`in_progress` was not in it either** — so the moment `in_progress` became
    automatic (2026-09-08), the second step completed on any case would have
    stopped auto-advancing. A hardcoded list that is wrong the day a feature
    ships is the argument for not having one.
    """
    found = await _status_of(session, case)
    if found is None:
        # A status nobody defined is still an OPEN case — the same answer
        # `is_terminal` gives, and for the same reason: treating it as finished
        # would silently stop work on it.
        return True
    return not found.is_terminal and found.category != "waiting"


async def is_concluded_for_case(session: AsyncSession, case: Any) -> bool:
    """Did this case END WELL — finished rather than abandoned?

    Terminal, minus `cancelled`. A distinct question from `is_terminal_for_case`
    and the difference matters: a cancelled case is over, so no more work
    happens on it, but it was not *concluded* — its progress rail should not
    read as complete and nobody should be asked to rate the service they
    received on it.

    Named here rather than written out at the three call sites that need it,
    because three copies of "terminal but not cancelled" is how the platform
    ended up with fourteen copies of "resolved, closed, cancelled".
    """
    found = await _status_of(session, case)
    return bool(found and found.is_terminal
                and getattr(case, "status", None) != "cancelled")


async def is_thread_closed_for_case(session: AsyncSession, case: Any) -> bool:
    """May a customer still write to this case?

    Terminal, minus `resolved` — the third distinct reading of "finished" on
    this platform, and the only one where `resolved` is deliberately still open.
    A customer replying to a resolution is how a case gets REOPENED; refusing
    that message would silently lose the one signal that says the resolution
    was wrong.
    """
    found = await _status_of(session, case)
    return bool(found and found.is_terminal
                and getattr(case, "status", None) != "resolved")


async def default_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    case_type_id: uuid.UUID | None = None,
) -> str:
    """The status a newly created case takes. `new` unless a set says otherwise."""
    statuses = await resolve_set(session, tenant_id=tenant_id, case_type_id=case_type_id)
    for s in sorted(statuses.values(), key=lambda x: x.sort_order):
        if s.is_default and s.enabled:
            return s.name
    return "new"


async def validate_transition(
    session: AsyncSession,
    *,
    current: str | None,
    target: str,
    tenant_id: uuid.UUID | None = None,
    case_type_id: uuid.UUID | None = None,
) -> Status:
    """Refuse a status write the resolved set does not permit, and say why.

    Three distinct refusals, each with its own code, because "that did not work"
    is not an answer an operator can act on:

    * the target is **not in the set** — a typo, or a status nobody defined;
    * the target is **disabled** — it exists, it is on rows already, but it is
      no longer offered for new transitions;
    * the target is **not reachable from here** — the transition graph says no,
      and the error names what it would accept.

    A `current` the set does not define is **not** an error. Those rows predate
    the set or survived a rename, and they must stay movable — refusing to let a
    case leave an undefined status would strand it there permanently.
    """
    statuses = await resolve_set(session, tenant_id=tenant_id, case_type_id=case_type_id)

    found = statuses.get(target)
    if found is None:
        known = ", ".join(sorted(s.name for s in statuses.values() if s.enabled))
        raise StatusError(
            f"'{target}' is not a status on this platform. Known: {known}.",
            code="VEL-CASE-STATUS-UNKNOWN-400",
        )
    if not found.enabled:
        raise StatusError(
            f"Status '{target}' is disabled and cannot be set on a case. "
            f"Cases already holding it are unaffected.",
            code="VEL-CASE-STATUS-DISABLED-409",
        )
    if not found.reachable_from(current):
        allowed = ", ".join(found.allowed_from)
        raise StatusError(
            f"A case cannot go from '{current}' to '{target}'. "
            f"'{target}' is reachable from: {allowed}.",
            code="VEL-CASE-STATUS-TRANSITION-409",
        )
    return found


def describe(statuses: dict[str, Status]) -> list[dict[str, Any]]:
    """The set as the API and the Studio config screen render it."""
    return [
        {
            "name": s.name, "label": s.label, "category": s.category,
            "is_terminal": s.is_terminal, "is_default": s.is_default,
            "allowed_from": list(s.allowed_from), "sort_order": s.sort_order,
            "enabled": s.enabled,
        }
        for s in sorted(statuses.values(), key=lambda x: (x.sort_order, x.name))
    ]
