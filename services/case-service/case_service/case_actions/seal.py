"""Archived and deleted cases are inert — enforced in ONE place.

Design: `docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md` §7, answered 2026-09-08.

**Why this module exists, and it is not a tidiness argument.** The seal was
first written inside the action dispatcher, which was enough to make every test
pass and enough to make the feature look finished. It was not finished: a live
probe on 2026-09-08 archived a case and then successfully moved it to another
stage (200), created an assignment on it (201) and shared it with another user
(201) — because those three routes do not go through the dispatcher. "Wrapped up
without any further changes" was true of eleven actions and false of every other
write on the case surface.

So the rule lives here and the write routes call it. The same lesson as
`require_case`, which grew a tenant clause on 2026-09-06 for exactly this
reason: a rule enforced at each call site is a rule that is missing at the call
site somebody adds next year.

**Two states, one door each.** An archived case accepts `reopen`; a deleted case
accepts `restore`. Everything else is refused. The exceptions are deliberate —
an action with literally no reverse is one an operator who archived the wrong
case can never undo, and "no way back" is a worse property for a control than
"the way back is privileged and audited".

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

#: The status that seals a case. A single name, deliberately: unlike
#: terminality — which a tenant may extend, and which `statuses.is_terminal`
#: therefore resolves from data — sealing is a PLATFORM guarantee about
#: immutability. A tenant that could nominate its own sealing status could make
#: any status stop the platform writing, including one cases sit on every day.
SEALED_STATUS = "archived"

#: The one action each sealed state accepts.
UNSEALS = {"archived": "reopen", "deleted": "restore"}

CODE = "VEL-CASE-ACTION-SEALED-409"


def sealed_reason(case: Any, *, action: str | None = None) -> str | None:
    """Why this case may not be written to, or None if it may.

    `action` is the action being attempted, where there is one; the two routes
    that reverse a seal pass their own name and are let through. A caller with
    no action name — an ordinary write route — is refused by any seal, which is
    the safe default: a route that has not thought about the seal should not be
    the one that decides it does not apply.
    """
    if case is None:
        return None
    if bool(getattr(case, "is_deleted", False)):
        if action == UNSEALS["deleted"]:
            return None
        return ("This case is deleted. Restore it first if it needs to change.")
    if (getattr(case, "status", None) or "") == SEALED_STATUS:
        if action == UNSEALS["archived"]:
            return None
        return ("This case is archived. Nothing further may be done to it; "
                "reopen it first if it needs to change.")
    return None


def assert_writable(case: Any, *, action: str | None = None) -> None:
    """Raise 409 if the case is sealed. For the routes outside the dispatcher.

    409 rather than 403: the caller's rights are not in question, the case's
    state is — and 403 would send an operator to an administrator asking for a
    permission that would not help them.
    """
    reason = sealed_reason(case, action=action)
    if reason is None:
        return
    from velaris_shared.core.error_codes import coded

    raise coded(409, CODE, reason)
