"""Authoring the status set and the action configuration a case type runs on.

Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md

**What a customer owns here, and what they do not.** Statuses entirely — names,
labels, categories, which end a case, which transitions are legal. Actions only
in presentation and reach: a custom label, hidden or shown, sort order, the
privilege that gates it — as an ADDITIONAL requirement, never a
replacement, so a configuration can only ever tighten access. The *behaviour* of
each action is a closed registry in code, because `escalate` fires a tree and
notifies, and that cannot be inferred from the word.

Two rules this router enforces that are easy to get wrong:

* **A row naming an action outside the registry is refused at WRITE time.**
  Ignoring it at read time would leave an operator staring at a configuration
  that silently does nothing — the worst of both answers.
* **A status a case is sitting on cannot be deleted.** Deleting it would make
  that case unreadable rather than making it correct. `enabled = false` is the
  verb for "stop offering this", and it leaves existing rows alone.

Gated on `case.actions.configure` — three segments, so it is grantable from data
rather than being tied to the `admin` role name.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.core.error_codes import coded
from velaris_shared.db.models import (
    CaseActionConfigModel,
    CaseActionConfigModel as _Cfg,
    CaseInstanceModel,
    CaseStatusModel,
)
from velaris_shared.db.session import get_session
from velaris_shared import hxguard
from velaris_shared.hxguard.actions import ACTIONS as _GUARD_ACTIONS

from case_service.case_actions import registry, statuses
from case_service.tenancy.identity import resolve_tenant

router = APIRouter(prefix="/case-config", tags=["case-config"])

_PRIV = "case.actions.configure"

#: The only categories a status may declare. Closed, because code reasons about
#: kinds of status (`is this being worked`) without knowing a tenant's names,
#: and a category nothing recognises would make those rules silently miss.
CATEGORIES = ("open", "working", "waiting", "terminal")


class StatusUpsert(BaseModel):
    name: str = Field(max_length=30)
    label: str = Field(max_length=100)
    category: str = "open"
    is_terminal: bool = False
    is_default: bool = False
    allowed_from: list[str] = Field(default_factory=list)
    sort_order: int = 0
    enabled: bool = True
    case_type_id: uuid.UUID | None = None


class ActionConfigUpsert(BaseModel):
    action: str = Field(max_length=40)
    label: str | None = Field(default=None, max_length=100)
    enabled: bool = True
    sort_order: int = 0
    privilege: str | None = Field(default=None, max_length=80)
    allowed_from: list[str] = Field(default_factory=list)
    case_type_id: uuid.UUID | None = None


async def _tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID | None:
    """The caller's own tenant, and never one they named.

    A configuration screen that accepted a `tenant_id` from the body would let
    any operator author another tenant's status set — the same shape as the
    upload route that believed a caller-supplied `tenant_id` until 2026-09-06.
    """
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None))
    return resolved[0] if resolved else None


async def _own_case_type(
    session: AsyncSession, tenant_uuid: uuid.UUID | None,
    case_type_id: uuid.UUID | None,
) -> None:
    """Refuse a `case_type_id` the caller cannot see.

    The row it would write is **inert** — its `scope_key` carries the caller's
    OWN tenant, so it can only ever resolve for that tenant's callers acting on
    that case type, and a caller who cannot reach the case type cannot reach its
    cases either. So this is not an escalation.

    It is closed anyway for two reasons. A 200 on an id the caller cannot see is
    a **confirmation oracle** — weak against UUIDs, but the platform's answer to
    "does this id exist" is 404 everywhere else and should not have an exception
    here. And a configuration screen listing rows against case types the
    operator cannot see is a support call waiting to happen.

    A NULL-tenant case type stays visible to everybody, which is what every
    release so far does and what single-tenant installs depend on.
    """
    if case_type_id is None:
        return
    from velaris_shared.db.models import CaseTypeModel

    row = (await session.execute(
        select(CaseTypeModel.id, CaseTypeModel.tenant_id)
        .where(CaseTypeModel.id == case_type_id)
    )).first()
    if row is None or (row[1] is not None and row[1] != tenant_uuid):
        # 404, never 403 — the posture the platform takes everywhere else.
        raise coded(404, "VEL-CASE-NOTFOUND-404",
                    "No such case type.")


# ── statuses ─────────────────────────────────────────────────────────────────

@router.get("/statuses")
async def list_statuses(
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The resolved set — what a case actually runs on, platform rows included."""
    await hxguard.require_user(session, user, _PRIV)
    tenant_uuid = await _tenant(session, user)
    await _own_case_type(session, tenant_uuid, case_type_id)
    resolved = await statuses.resolve_set(
        session, tenant_id=tenant_uuid, case_type_id=case_type_id)
    return {"statuses": statuses.describe(resolved), "categories": list(CATEGORIES)}


@router.put("/statuses")
async def upsert_status(
    body: StatusUpsert,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await hxguard.require_user(session, user, _PRIV)
    tenant_uuid = await _tenant(session, user)
    if tenant_uuid is None:
        # Writing a platform-wide row from a tenant screen would change every
        # tenant's status set at once. `arbiter_namespaces` is not tenant-scoped
        # and a promotion there is a platform-wide act; that lesson applies here.
        raise coded(400, "VEL-CASE-CONFIG-TENANT-400",
            "A status set is authored per tenant. This caller resolves to no "
            "tenant, and writing the platform default set from here would "
            "change every tenant at once.")

    name = body.name.strip().lower()
    if not name or not name.replace("_", "").isalnum():
        raise coded(400, "VEL-CASE-STATUS-NAME-400",
            "A status name is letters, digits and underscores — it is stored on "
            "every case row and appears in every audit entry.")
    if body.category not in CATEGORIES:
        raise coded(400, "VEL-CASE-STATUS-CATEGORY-400",
            f"'{body.category}' is not a category. Known: {', '.join(CATEGORIES)}.")

    await _own_case_type(session, tenant_uuid, body.case_type_id)

    key = statuses.scope_key(tenant_uuid, body.case_type_id)
    row = (await session.execute(
        select(CaseStatusModel).where(
            CaseStatusModel.scope_key == key, CaseStatusModel.name == name)
    )).scalars().first()

    if row is None:
        row = CaseStatusModel(
            tenant_id=tenant_uuid, case_type_id=body.case_type_id,
            scope_key=key, name=name)
        session.add(row)

    row.label = body.label.strip() or name
    row.category = body.category
    row.is_terminal = bool(body.is_terminal)
    row.is_default = bool(body.is_default)
    row.allowed_from = [str(x).strip().lower() for x in body.allowed_from if str(x).strip()]
    row.sort_order = int(body.sort_order)
    row.enabled = bool(body.enabled)

    # At most one default per scope. Two defaults would make "what status does a
    # new case take" depend on row order, which is not an answer.
    if row.is_default:
        others = (await session.execute(
            select(CaseStatusModel).where(
                CaseStatusModel.scope_key == key,
                CaseStatusModel.name != name,
                CaseStatusModel.is_default.is_(True))
        )).scalars().all()
        for other in others:
            other.is_default = False

    await session.commit()
    return {"status": row.name, "scope": key}


@router.delete("/statuses/{name}", status_code=204)
async def delete_status(
    name: str,
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Remove a status, unless a case is sitting on it.

    A case whose status no longer exists is not corrected by the deletion — it
    is stranded, with nothing to label it and no transition graph to move it.
    `enabled = false` is the verb for "stop offering this"; it leaves the rows
    that already hold it working exactly as they do.
    """
    await hxguard.require_user(session, user, _PRIV)
    tenant_uuid = await _tenant(session, user)
    await _own_case_type(session, tenant_uuid, case_type_id)
    key = statuses.scope_key(tenant_uuid, case_type_id)

    row = (await session.execute(
        select(CaseStatusModel).where(
            CaseStatusModel.scope_key == key, CaseStatusModel.name == name)
    )).scalars().first()
    if row is None:
        raise coded(404, "VEL-CASE-STATUS-UNKNOWN-400",
            f"No status '{name}' in this scope.")

    in_use = (await session.execute(
        select(func.count()).select_from(CaseInstanceModel).where(
            CaseInstanceModel.status == name,
            CaseInstanceModel.tenant_id == str(tenant_uuid) if tenant_uuid else True,
        )
    )).scalar() or 0
    if in_use:
        raise coded(409, "VEL-CASE-STATUS-INUSE-409",
            f"{in_use} case(s) are on '{name}'. Deleting it would strand them "
            f"with no label and no way out. Disable it instead.")

    await session.delete(row)
    await session.commit()


# ── action configuration ─────────────────────────────────────────────────────

@router.get("/actions")
async def list_action_config(
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The registry, with this scope's overrides folded in.

    Returns every action including disabled ones, unlike `/cases/actions/catalogue`
    which is what a case screen renders. This is the CONFIGURATION screen: an
    operator has to be able to see and re-enable what they switched off.
    """
    await hxguard.require_user(session, user, _PRIV)
    from case_service.case_actions import dispatcher as _disp

    tenant_uuid = await _tenant(session, user)
    await _own_case_type(session, tenant_uuid, case_type_id)
    cfg = await _disp.resolve_config(
        session, tenant_id=tenant_uuid, case_type_id=case_type_id)

    out: list[dict[str, Any]] = []
    for name in registry.names():
        entry = registry.ACTIONS[name].describe()
        row = cfg.get(name)
        entry["configured"] = row is not None
        entry["enabled"] = bool(row.enabled) if row else True
        entry["label"] = (row.label if row and row.label else entry["label"])
        # The override ADDS. Showing it alone would tell an operator they had
        # replaced the gate when in fact they tightened it.
        entry["required_privileges"] = list(_disp.required_privileges(
            registry.ACTIONS[name], row))
        entry["privilege"] = entry["required_privileges"][0]
        entry["extra_privilege"] = (row.privilege if row else None)
        entry["allowed_from"] = list(row.allowed_from or []) if row else []
        entry["sort_order"] = row.sort_order if row else 0
        out.append(entry)
    out.sort(key=lambda a: (a["sort_order"], a["name"]))
    return {"actions": out}


@router.put("/actions")
async def upsert_action_config(
    body: ActionConfigUpsert,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await hxguard.require_user(session, user, _PRIV)

    # Refused at write time, not ignored at read time. A configuration row that
    # silently does nothing is worse than a refusal, because the operator
    # believes they have configured something.
    if registry.get(body.action) is None:
        raise coded(400, "VEL-CASE-ACTION-UNKNOWN-404",
            f"'{body.action}' is not a case action. Known: "
            f"{', '.join(registry.names())}. The action vocabulary is closed — "
            f"new BEHAVIOUR is a rule or an intent fragment, not a config row.")

    if body.privilege is not None and body.privilege not in _GUARD_ACTIONS:
        raise coded(400, "VEL-CASE-ACTION-PRIV-400",
            f"'{body.privilege}' is not a privilege this platform evaluates. "
            f"An unknown privilege denies everything, which would silently "
            f"disable the action for everybody. Note this privilege is required "
            f"IN ADDITION to the action's own — it can only narrow access.")

    tenant_uuid = await _tenant(session, user)
    if tenant_uuid is None:
        raise coded(400, "VEL-CASE-CONFIG-TENANT-400",
            "Action configuration is authored per tenant. This caller resolves "
            "to no tenant, and writing the platform row from here would change "
            "every tenant at once.")

    await _own_case_type(session, tenant_uuid, body.case_type_id)

    key = statuses.scope_key(tenant_uuid, body.case_type_id)
    row = (await session.execute(
        select(_Cfg).where(_Cfg.scope_key == key, _Cfg.action == body.action)
    )).scalars().first()
    if row is None:
        row = _Cfg(tenant_id=tenant_uuid, case_type_id=body.case_type_id,
                   scope_key=key, action=body.action)
        session.add(row)

    row.label = (body.label or "").strip() or None
    row.enabled = bool(body.enabled)
    row.sort_order = int(body.sort_order)
    row.privilege = body.privilege
    row.allowed_from = [str(x).strip().lower() for x in body.allowed_from if str(x).strip()]

    await session.commit()
    return {"action": row.action, "scope": key, "enabled": row.enabled}


@router.delete("/actions/{action}", status_code=204)
async def clear_action_config(
    action: str,
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Drop the override; the action returns to the registry's own defaults."""
    await hxguard.require_user(session, user, _PRIV)
    tenant_uuid = await _tenant(session, user)
    await _own_case_type(session, tenant_uuid, case_type_id)
    key = statuses.scope_key(tenant_uuid, case_type_id)
    row = (await session.execute(
        select(_Cfg).where(_Cfg.scope_key == key, _Cfg.action == action)
    )).scalars().first()
    if row is None:
        raise coded(404, "VEL-CASE-ACTION-UNKNOWN-404", f"No override for '{action}'.")
    await session.delete(row)
    await session.commit()
