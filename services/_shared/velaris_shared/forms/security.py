"""Field-level security — who sees a field and who may change it (Form Builder v2 §5 step 7, 2026-10-29).

A field may carry `security: {"view": [role, …], "edit": [role, …]}`, naming
access roles (Admin Studio > Access Roles, by id or by name):

* `view` set  — only a caller holding one of those roles sees the field;
* `edit` set  — only a caller holding one of those roles may change it; anyone
  else who sees it sees it read-only. Editing implies seeing.
* absent or both empty — everyone, exactly as before.

The server applies it twice, as the proposal says (§4):

1. **Serving** (`for_browser=True`): a field the caller may not see is left out
   of the definition; one they may not change comes marked `read_only`.
2. **Accepting**: the definition the validator runs is the same one with every
   restricted field read-only and without its default, so whatever was sent for
   it is dropped and the case keeps what it holds. A rule or a formula reads
   the case's value for it — never the caller's.

The roles are those of the caller's active access groups that may act on the
case (its tenant and case type) — the groups `step_submit` admits by.
`superadmin` sees and changes everything (`None` = not restricted).

Not covered: values already in case data stay readable wherever case data is
shown (Case Manager's data tab, case variables). This hides and locks the field
in the form; it does not redact the case.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import copy
from typing import Any

EDIT, VIEW, NONE = "edit", "view", "none"


def _names(raw: Any) -> list[str]:
    return [str(r) for r in raw if isinstance(r, (str, int)) and str(r).strip()] if isinstance(raw, list) else []


def restricted(field: dict[str, Any]) -> bool:
    """The field limits who sees or changes it."""
    sec = field.get("security")
    return isinstance(sec, dict) and bool(_names(sec.get("view")) or _names(sec.get("edit")))


def field_access(field: dict[str, Any], roles: set[str] | None) -> str:
    """`edit`, `view` or `none` for a caller holding `roles` (None = unrestricted)."""
    if roles is None or not restricted(field):
        return EDIT
    sec = field["security"]
    view, edit = set(_names(sec.get("view"))), set(_names(sec.get("edit")))
    if edit and roles & edit:
        return EDIT
    if view and not roles & view:
        return NONE
    return VIEW if edit else EDIT


def _lock(field: dict[str, Any]) -> dict[str, Any]:
    out = dict(field)
    out["read_only"] = True
    out.pop("default_value", None)
    out.pop("read_only_when", None)
    out.pop("required_when", None)
    out["required"] = False
    return out


def _secure_fields(fields: Any, roles: set[str], for_browser: bool) -> list[Any]:
    out: list[Any] = []
    for f in fields if isinstance(fields, list) else []:
        if not isinstance(f, dict):
            out.append(f)
            continue
        access = field_access(f, roles)
        if access == NONE and for_browser:
            continue
        g = f if access == EDIT else _lock(f)
        if isinstance(g.get("fields"), list):                 # a repeating group's columns
            g = dict(g)
            g["fields"] = _secure_fields(g["fields"], roles, for_browser)
        out.append(g)
    return out


def secure_definition(definition: Any, roles: set[str] | None, *, for_browser: bool) -> Any:
    """The definition as this caller has it. `roles` None = unchanged (the same object)."""
    if roles is None or not isinstance(definition, dict) or not any_restricted(definition):
        return definition
    out = copy.copy(definition)
    sections = []
    for sec in definition.get("sections") if isinstance(definition.get("sections"), list) else []:
        if isinstance(sec, dict):
            sec = dict(sec)
            sec["fields"] = _secure_fields(sec.get("fields"), roles, for_browser)
        sections.append(sec)
    out["sections"] = sections
    return out


def any_restricted(definition: Any) -> bool:
    for sec in definition.get("sections") or [] if isinstance(definition, dict) else []:
        for f in (sec.get("fields") or []) if isinstance(sec, dict) else []:
            if isinstance(f, dict) and (restricted(f) or any(
                    isinstance(c, dict) and restricted(c) for c in (f.get("fields") or []))):
                return True
    return False


async def caller_roles(session: Any, user: Any, case: Any) -> set[str] | None:
    """The access roles (ids and names) the caller holds for this case; None = superadmin."""
    from sqlalchemy import select

    from velaris_shared.db.models import AccessRoleModel
    from velaris_shared.hxguard.step_submit import _groups_of, _identities, _usable
    from velaris_shared.tenancy.identity import is_superadmin
    if is_superadmin(user):
        return None
    groups = await _groups_of(session, _identities(user))
    if case is not None:
        groups = await _usable(session, groups, case)
    ids = {str(r) for g in groups for r in (g.role_ids or [])}
    import uuid as _uuid
    uuids = []
    for r in ids:
        try:
            uuids.append(_uuid.UUID(r))
        except ValueError:
            continue
    names = set((await session.execute(
        select(AccessRoleModel.name).where(AccessRoleModel.id.in_(uuids))
    )).scalars().all()) if uuids else set()
    return ids | {str(n) for n in names}
