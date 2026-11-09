"""Who may create a case — decided by the access roles defined in Admin Studio.

Until 2026-10-18 `POST /api/v1/cases` checked only that the caller was signed
in, so a `viewer` could open cases. The rule now (user's decision, 2026-10-18):

* A caller in an **access group** may create a case of type T only when
  - T is among the group's allowed case types (`allowed_case_type_ids`,
    `["*"]` = all), and
  - a role in that group grants `case` → `create` for T: an entry whose
    `resource` is `case` (or `*`), whose `actions` hold `create` (or `*`), and
    whose `case_type_id` is absent, `*`, or T. An entry with
    `"effect": "deny"` that matches refuses, whatever else allows.
* A caller in **no** access group is refused until an administrator assigns
  one. Deliberately strict: Admin Studio is the one place this is decided.
* `superadmin` passes — the break-glass every other gate keeps, so a bad
  access-role configuration stays recoverable.

The privileges read here are the access group's own (`active_access_group`),
never the platform roles on the token: an access role cannot reach a platform
action, and a platform role does not stand in for an access role.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

NO_GROUP = (
    "You are not in an access group, so you cannot create cases. An administrator "
    "assigns one in Admin Studio > Access Groups."
)


def _matches(entry: dict, case_type_id: str) -> bool:
    if str(entry.get("resource", "")) not in ("case", "*"):
        return False
    actions = entry.get("actions") or []
    if "create" not in actions and "*" not in actions:
        return False
    scope = entry.get("case_type_id")
    if scope in (None, "", "*"):
        return True
    if isinstance(scope, (list, tuple, set)):
        return "*" in scope or case_type_id in {str(s) for s in scope}
    return str(scope) == case_type_id


def case_create_decision(user: Any, case_type_id: Any) -> tuple[bool, str]:
    """`(allowed, reason)`. Pure: everything it needs is already on the user."""
    if "superadmin" in (getattr(user, "roles", None) or []):
        return True, "superadmin break-glass"
    ag = getattr(user, "active_access_group", None)
    if ag is None:
        return False, NO_GROUP
    ct = str(case_type_id)
    allowed_types = [str(t) for t in (getattr(ag, "allowed_case_type_ids", None) or ["*"])]
    if "*" not in allowed_types and ct not in allowed_types:
        return False, (f"Your access group '{ag.name}' does not include this case type.")
    granted = False
    for entry in getattr(ag, "privileges", None) or []:
        if not isinstance(entry, dict) or not _matches(entry, ct):
            continue
        if str(entry.get("effect", "allow")).lower() == "deny":
            return False, f"Your access group '{ag.name}' denies creating this case type."
        granted = True
    if granted:
        return True, f"access group '{ag.name}' grants case create"
    return False, (f"No role in your access group '{ag.name}' allows creating cases "
                   "of this type. An administrator can grant Cases > Create in Admin "
                   "Studio > Access Roles.")


async def require_case_create(session: Any, user: Any, case_type_id: Any) -> None:
    """403 with the reason when `case_create_decision` refuses.

    A refusal is written as the same throttled `authz_denied` SecurityEvent
    every other HxGuard gate writes, so HxShield sees it.
    """
    ok, reason = case_create_decision(user, case_type_id)
    if ok:
        return
    try:
        from velaris_shared.hxguard.service import Decision, _maybe_log_deny, subject_from_user
        await _maybe_log_deny(session, subject_from_user(user), "case.create",
                              {"case_type_id": str(case_type_id)},
                              Decision(False, reason, "access_group"), "authz_denied")
    except Exception:                                   # the audit never decides
        pass
    raise HTTPException(status_code=403, detail=reason)
