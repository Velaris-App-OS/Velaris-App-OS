"""User, case and document pickers — which ids a caller may choose (Form Builder batch M, 2026-10-29).

A picker stores an id. What the browser offers comes from
`POST /forms/picker-options`; what the server accepts is decided here, from the
same rules, for exactly the ids that were submitted:

- **user_picker** — an active person in the caller's tenant (`user_directory`).
- **case_picker** — a case of the caller's tenant, not deleted.

A row with no tenant predates multi-tenancy and belongs to the default tenant:
it is offered and accepted for a default-tenant caller only, never for another
tenant (found live 2026-10-29: 28 of 30 directory rows on the dev box had none).
- **document_picker** — a document, not deleted, of the case the form is for.

Superadmin is held to the same lists: a form is filled in one tenant.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select

from velaris_shared.forms.validate import PICKER_KINDS, _fields, field_key_of

#: How many options a search returns.
OPTIONS_LIMIT = 20


def _submitted(definition: Any, values: dict[str, Any]) -> dict[str, set[str]]:
    """The ids submitted for each picker kind, repeating-group rows included."""
    out: dict[str, set[str]] = {k: set() for k in PICKER_KINDS.values()}
    for f in _fields(definition):
        key = field_key_of(f)
        if f.get("type") in PICKER_KINDS and isinstance(values.get(key), str):
            out[PICKER_KINDS[f["type"]]].add(values[key])
        if f.get("type") == "repeating_group" and isinstance(values.get(key), list):
            kids = [c for c in (f.get("fields") or []) if isinstance(c, dict) and c.get("type") in PICKER_KINDS]
            for row in values[key][:500]:
                if not isinstance(row, dict):
                    continue
                for c in kids:
                    v = row.get(field_key_of(c))
                    if isinstance(v, str):
                        out[PICKER_KINDS[c["type"]]].add(v)
    return out


async def _default_caller(session: Any, user: Any) -> bool:
    from velaris_shared.tenancy.identity import DEFAULT_TENANT, same_tenant_as_default
    return await same_tenant_as_default(session, getattr(user, "tenant_id", None) or DEFAULT_TENANT)


def _uuids(ids: set[str]) -> list[uuid.UUID]:
    out = []
    for i in ids:
        try:
            out.append(uuid.UUID(i))
        except ValueError:
            pass
    return out


async def picker_lookups(session: Any, definition: Any, values: dict[str, Any] | None, *,
                         user: Any, case_id: Any = None) -> dict[str, set[str]]:
    """{kind: the submitted ids this caller may choose}. Ids not returned are refused."""
    from velaris_shared.db.models import CaseInstanceModel, DocumentModel, UserDirectoryModel
    from velaris_shared.tenancy.identity import caller_tenant_tokens, caller_tenant_uuid

    sub = _submitted(definition, values or {})
    allowed: dict[str, set[str]] = {k: set() for k in sub}
    if sub["user"]:
        tokens = await caller_tenant_tokens(session, user) or []
        mine_u = UserDirectoryModel.tenant_id.in_(tokens)
        if await _default_caller(session, user):
            mine_u = or_(mine_u, UserDirectoryModel.tenant_id.is_(None))
        rows = (await session.execute(select(UserDirectoryModel.user_id).where(
            UserDirectoryModel.user_id.in_(sub["user"]),
            mine_u,
            UserDirectoryModel.is_active.is_not(False),
        ))).scalars().all()
        allowed["user"] = set(rows)
    if sub["case"]:
        mine = await caller_tenant_uuid(session, user)
        ids = _uuids(sub["case"])
        if mine is not None and ids:
            mine_c = CaseInstanceModel.tenant_id == mine
            if await _default_caller(session, user):
                mine_c = or_(mine_c, CaseInstanceModel.tenant_id.is_(None))
            rows = (await session.execute(select(CaseInstanceModel.id).where(
                CaseInstanceModel.id.in_(ids), mine_c,
                CaseInstanceModel.is_deleted.is_(False),
            ))).scalars().all()
            allowed["case"] = {str(r) for r in rows}
    if sub["document"] and case_id is not None:
        ids = _uuids(sub["document"])
        if ids:
            rows = (await session.execute(select(DocumentModel.id).where(
                DocumentModel.id.in_(ids), DocumentModel.case_id == uuid.UUID(str(case_id)),
                DocumentModel.is_deleted.is_(False),
            ))).scalars().all()
            allowed["document"] = {str(r) for r in rows}
    return allowed


async def picker_options(session: Any, kind: str, q: str, *, user: Any, case_id: Any = None) -> list[dict[str, str]]:
    """What a picker offers: the same rules as `picker_lookups`, searched."""
    from velaris_shared.db.models import CaseInstanceModel, DocumentModel, UserDirectoryModel
    from velaris_shared.tenancy.identity import caller_tenant_tokens, caller_tenant_uuid

    q = (q or "").strip()[:100]
    like = f"%{q.replace('%', '').replace('_', '')}%"
    if kind == "user":
        if len(q) < 2:
            return []
        tokens = await caller_tenant_tokens(session, user) or []
        mine_u = UserDirectoryModel.tenant_id.in_(tokens)
        if await _default_caller(session, user):
            mine_u = or_(mine_u, UserDirectoryModel.tenant_id.is_(None))
        rows = (await session.execute(select(UserDirectoryModel).where(
            mine_u, UserDirectoryModel.is_active.is_not(False),
            or_(UserDirectoryModel.display_name.ilike(like), UserDirectoryModel.user_id.ilike(like),
                UserDirectoryModel.email.ilike(like)),
        ).order_by(UserDirectoryModel.display_name).limit(OPTIONS_LIMIT))).scalars().all()
        return [{"value": r.user_id, "label": r.display_name or r.user_id} for r in rows]
    if kind == "case":
        mine = await caller_tenant_uuid(session, user)
        if mine is None or len(q) < 2:
            return []
        mine_c = CaseInstanceModel.tenant_id == mine
        if await _default_caller(session, user):
            mine_c = or_(mine_c, CaseInstanceModel.tenant_id.is_(None))
        rows = (await session.execute(select(CaseInstanceModel).where(
            mine_c, CaseInstanceModel.is_deleted.is_(False),
            CaseInstanceModel.case_number.ilike(like),
        ).order_by(CaseInstanceModel.created_at.desc()).limit(OPTIONS_LIMIT))).scalars().all()
        return [{"value": str(r.id), "label": f"{r.case_number} · {r.status}"} for r in rows]
    if kind == "document":
        if case_id is None:
            return []
        rows = (await session.execute(select(DocumentModel).where(
            DocumentModel.case_id == uuid.UUID(str(case_id)), DocumentModel.is_deleted.is_(False),
            DocumentModel.filename.ilike(like),
        ).order_by(DocumentModel.created_at.desc()).limit(50))).scalars().all()
        return [{"value": str(r.id), "label": r.filename} for r in rows]
    return []
