"""Case Variables Phase 1 API (spec v2 — docs/Future/case_variables.md).

Registry admin (admin), variable definitions for the Case Designer (designer),
and redacted instance reads (any authenticated operator). Instance WRITES are
not exposed over HTTP in Phase 1 — integrations write through the case_vars
service in Phase 2, with namespaces derived from their connector identity.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service import hxguard
from case_service.auth.models import AuthenticatedUser
from case_service.case_vars import CallerContext, VariableError
from case_service.case_vars import get_all as vars_get_all
from case_service.case_vars.service import (
    NAME_RE,
    RESERVED_NAMESPACES,
    SENSITIVITY_ORDER,
)
from case_service.db.models import (
    CaseTypeVariableModel,
    NamespaceGrantModel,
    VariableNamespaceModel,
)
from case_service.db.session import get_session

router = APIRouter(prefix="/variables", tags=["case-variables"])

VAR_TYPES = {"str", "int", "float", "bool", "date", "datetime", "list", "dict", "any"}
OWNER_TYPES = {"connector", "devconn"}   # registrable via API; platform/form/portal are seeded


def _operator_ctx(user: AuthenticatedUser) -> CallerContext:
    # mirrors _has_sensitive_access in the cases router
    privileged = user.is_admin or "finance" in (user.roles or []) or "admin" in (user.roles or [])
    return CallerContext(kind="operator", actor_id=user.user_id, privileged=privileged)


# ── Namespace registry (admin) ───────────────────────────────────────

class NamespaceCreate(BaseModel):
    name: str = Field(..., max_length=100)
    owner_type: str
    owner_ref: Optional[uuid.UUID] = None
    sensitivity: str = "internal"


class NamespacePatch(BaseModel):
    sensitivity: Optional[str] = None
    status: Optional[str] = None   # active | frozen | retired


def _ns_out(n: VariableNamespaceModel) -> dict:
    return {
        "id": str(n.id), "name": n.name, "owner_type": n.owner_type,
        "owner_ref": str(n.owner_ref) if n.owner_ref else None,
        "sensitivity": n.sensitivity, "status": n.status,
        "created_by": n.created_by,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "reserved": n.name in RESERVED_NAMESPACES,
    }


@router.get("/namespaces")
async def list_namespaces(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rows = (await session.execute(
        select(VariableNamespaceModel).order_by(VariableNamespaceModel.name)
    )).scalars().all()
    return [_ns_out(n) for n in rows]


@router.post("/namespaces", status_code=201)
async def create_namespace(
    body: NamespaceCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("variables.namespace.admin")),
):
    name = (body.name or "").strip().lower()
    if not NAME_RE.match(name):
        raise HTTPException(400, "Namespace must match ^[a-z][a-z0-9_]{0,99}$")
    if name in RESERVED_NAMESPACES:
        raise HTTPException(400, f"'{name}' is a reserved namespace")
    if body.owner_type not in OWNER_TYPES:
        raise HTTPException(400, f"owner_type must be one of {sorted(OWNER_TYPES)}")
    if body.sensitivity not in SENSITIVITY_ORDER:
        raise HTTPException(400, f"sensitivity must be one of {SENSITIVITY_ORDER}")

    ns = VariableNamespaceModel(
        id=uuid.uuid4(), name=name, owner_type=body.owner_type,
        owner_ref=body.owner_ref, sensitivity=body.sensitivity,
        created_by=user.user_id,
    )
    session.add(ns)
    try:
        await session.flush()
    except Exception:
        raise HTTPException(409, f"Namespace '{name}' already exists")
    return _ns_out(ns)


@router.patch("/namespaces/{namespace_id}")
async def patch_namespace(
    namespace_id: uuid.UUID,
    body: NamespacePatch,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("variables.namespace.admin")),
):
    ns = await session.get(VariableNamespaceModel, namespace_id)
    if ns is None:
        raise HTTPException(404, "Namespace not found")
    if ns.name in RESERVED_NAMESPACES:
        raise HTTPException(400, "Reserved namespaces cannot be modified")
    if body.sensitivity is not None:
        if body.sensitivity not in SENSITIVITY_ORDER:
            raise HTTPException(400, f"sensitivity must be one of {SENSITIVITY_ORDER}")
        ns.sensitivity = body.sensitivity
    if body.status is not None:
        if body.status not in ("active", "frozen", "retired"):
            raise HTTPException(400, "status must be active | frozen | retired")
        ns.status = body.status
    return _ns_out(ns)


class GrantCreate(BaseModel):
    grantee_type: str   # connector | devconn | rules | module
    grantee_ref: str = Field(..., max_length=255)
    capability: str     # read | write


@router.get("/namespaces/{namespace_id}/grants")
async def list_grants(
    namespace_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("variables.namespace.admin")),
):
    rows = (await session.execute(
        select(NamespaceGrantModel).where(NamespaceGrantModel.namespace_id == namespace_id)
    )).scalars().all()
    return [
        {"id": str(g.id), "grantee_type": g.grantee_type, "grantee_ref": g.grantee_ref,
         "capability": g.capability, "granted_by": g.granted_by,
         "granted_at": g.granted_at.isoformat() if g.granted_at else None}
        for g in rows
    ]


@router.post("/namespaces/{namespace_id}/grants", status_code=201)
async def create_grant(
    namespace_id: uuid.UUID,
    body: GrantCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("variables.namespace.admin")),
):
    if body.capability not in ("read", "write"):
        raise HTTPException(400, "capability must be read | write")
    if body.grantee_type not in ("connector", "devconn", "rules", "module"):
        raise HTTPException(400, "invalid grantee_type")
    if await session.get(VariableNamespaceModel, namespace_id) is None:
        raise HTTPException(404, "Namespace not found")
    grant = NamespaceGrantModel(
        id=uuid.uuid4(), namespace_id=namespace_id,
        grantee_type=body.grantee_type, grantee_ref=body.grantee_ref,
        capability=body.capability, granted_by=user.user_id,
    )
    session.add(grant)
    try:
        await session.flush()
    except Exception:
        raise HTTPException(409, "Grant already exists")
    hxguard.invalidate_cache()   # grant mutations invalidate cached decisions
    return {"id": str(grant.id)}


@router.delete("/namespaces/{namespace_id}/grants/{grant_id}", status_code=204)
async def delete_grant(
    namespace_id: uuid.UUID,
    grant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("variables.namespace.admin")),
):
    g = await session.get(NamespaceGrantModel, grant_id)
    if g is None or g.namespace_id != namespace_id:
        raise HTTPException(404, "Grant not found")
    await session.delete(g)
    hxguard.invalidate_cache()   # grant mutations invalidate cached decisions


# ── Variable definitions (Case Designer) ─────────────────────────────

class VariableDefine(BaseModel):
    namespace_id: uuid.UUID
    name: str = Field(..., max_length=100)
    var_type: str = "str"
    label: Optional[str] = None
    description: Optional[str] = None
    required: bool = False
    indexed: bool = False
    sensitivity_override: Optional[str] = None


class VariablePatch(BaseModel):
    var_type: Optional[str] = None
    definition_status: Optional[str] = None   # defined | ignored (promote / suppress)
    label: Optional[str] = None
    description: Optional[str] = None
    required: Optional[bool] = None
    indexed: Optional[bool] = None
    sensitivity_override: Optional[str] = None


def _def_out(d: CaseTypeVariableModel, ns_name: str | None = None) -> dict:
    return {
        "id": str(d.id), "case_type_id": str(d.case_type_id),
        "namespace_id": str(d.namespace_id), "namespace": ns_name,
        "name": d.name, "full_key": d.full_key, "var_type": d.var_type,
        "definition_status": d.definition_status,
        "sensitivity_override": d.sensitivity_override,
        "label": d.label, "description": d.description,
        "required": d.required, "indexed": d.indexed,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _check_override(override: str | None, ns: VariableNamespaceModel) -> None:
    if override is None:
        return
    if override not in SENSITIVITY_ORDER:
        raise HTTPException(400, f"sensitivity_override must be one of {SENSITIVITY_ORDER}")
    if SENSITIVITY_ORDER.index(override) < SENSITIVITY_ORDER.index(ns.sensitivity):
        raise HTTPException(400, "sensitivity_override may only be STRICTER than the namespace")


@router.get("/case-types/{case_type_id}")
async def list_case_type_variables(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    defs = (await session.execute(
        select(CaseTypeVariableModel, VariableNamespaceModel.name)
        .join(VariableNamespaceModel, CaseTypeVariableModel.namespace_id == VariableNamespaceModel.id)
        .where(CaseTypeVariableModel.case_type_id == case_type_id)
        .order_by(CaseTypeVariableModel.full_key)
    )).all()
    return [_def_out(d, ns_name) for d, ns_name in defs]


@router.post("/case-types/{case_type_id}", status_code=201)
async def define_variable(
    case_type_id: uuid.UUID,
    body: VariableDefine,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("designer")),
):
    name = (body.name or "").strip().lower()
    if not NAME_RE.match(name):
        raise HTTPException(400, "Variable name must match ^[a-z][a-z0-9_]{0,99}$")
    if body.var_type not in VAR_TYPES:
        raise HTTPException(400, f"var_type must be one of {sorted(VAR_TYPES)}")
    ns = await session.get(VariableNamespaceModel, body.namespace_id)
    if ns is None:
        raise HTTPException(404, "Namespace not found")
    if ns.name == "velaris":
        raise HTTPException(400, "velaris.* variables are virtual — they are the case's native fields")
    if ns.status != "active":
        raise HTTPException(400, f"Namespace '{ns.name}' is {ns.status}")
    _check_override(body.sensitivity_override, ns)

    d = CaseTypeVariableModel(
        id=uuid.uuid4(), case_type_id=case_type_id, namespace_id=ns.id,
        name=name, full_key=f"{ns.name}.{name}", var_type=body.var_type,
        label=body.label or name.replace("_", " ").title(),
        description=body.description, required=body.required,
        indexed=body.indexed, sensitivity_override=body.sensitivity_override,
    )
    session.add(d)
    try:
        await session.flush()
    except Exception:
        raise HTTPException(409, f"Variable '{d.full_key}' already defined for this case type")
    return _def_out(d, ns.name)


@router.patch("/case-types/{case_type_id}/{variable_id}")
async def patch_variable(
    case_type_id: uuid.UUID,
    variable_id: uuid.UUID,
    body: VariablePatch,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("designer")),
):
    d = await session.get(CaseTypeVariableModel, variable_id)
    if d is None or d.case_type_id != case_type_id:
        raise HTTPException(404, "Variable not found")
    if body.var_type is not None:
        if body.var_type not in VAR_TYPES:
            raise HTTPException(400, f"var_type must be one of {sorted(VAR_TYPES)}")
        d.var_type = body.var_type
    if body.definition_status is not None:
        if body.definition_status not in ("defined", "ignored"):
            raise HTTPException(400, "definition_status must be defined | ignored")
        d.definition_status = body.definition_status   # promote or suppress — no data moves
    if body.sensitivity_override is not None:
        ns = await session.get(VariableNamespaceModel, d.namespace_id)
        _check_override(body.sensitivity_override, ns)
        d.sensitivity_override = body.sensitivity_override
    if body.label is not None: d.label = body.label
    if body.description is not None: d.description = body.description
    if body.required is not None: d.required = body.required
    if body.indexed is not None: d.indexed = body.indexed
    ns = await session.get(VariableNamespaceModel, d.namespace_id)
    return _def_out(d, ns.name if ns else None)


@router.delete("/case-types/{case_type_id}/{variable_id}", status_code=204)
async def delete_variable(
    case_type_id: uuid.UUID,
    variable_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("designer")),
):
    d = await session.get(CaseTypeVariableModel, variable_id)
    if d is None or d.case_type_id != case_type_id:
        raise HTTPException(404, "Variable not found")
    await session.delete(d)


# ── Instance reads (redaction enforced in the service layer) ─────────

@router.get("/cases/{case_id}")
async def get_case_variables(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        values = await vars_get_all(session, _operator_ctx(user), case_id)
    except VariableError as e:
        raise HTTPException(400, str(e))
    return {"case_id": str(case_id), "variables": values}


# ── case.data promotion (Phase 4 — migration wizard) ─────────────────

@router.get("/case-types/{case_type_id}/blob-keys")
async def scan_case_data_keys(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("variables.scan")),
):
    """Inventory bare case.data keys for the migration wizard: occurrence
    count, inferred type, HxSync pii hint, and promotion status."""
    from case_service.case_vars.service import scan_blob_keys
    return {
        "case_type_id": str(case_type_id),
        "keys": await scan_blob_keys(session, case_type_id),
    }


class PromoteRequest(BaseModel):
    key: str = Field(..., max_length=255)
    target_namespace: str = "legacy"
    target_name: Optional[str] = None    # required when key fails the name regex
    var_type: Optional[str] = None       # inferred from values when omitted
    indexed: bool = False


@router.post("/case-types/{case_type_id}/promote")
async def promote_case_data_key(
    case_type_id: uuid.UUID,
    body: PromoteRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("variables.promote")),
):
    """Promote one case.data key into a typed variable (admin only).

    The only write path into the reserved 'legacy' namespace. pii carry-over
    from HxSync classifications is forced server-side (stricter-only).
    """
    if body.var_type is not None and body.var_type not in VAR_TYPES:
        raise HTTPException(400, f"var_type must be one of {sorted(VAR_TYPES)}")
    from case_service.case_vars.service import promote_blob_key
    try:
        result = await promote_blob_key(
            session,
            case_type_id=case_type_id,
            key=body.key,
            target_namespace=body.target_namespace,
            target_name=body.target_name,
            var_type=body.var_type,
            indexed=body.indexed,
            actor=user.user_id,
        )
    except VariableError as e:
        raise HTTPException(400, str(e))
    return {"case_type_id": str(case_type_id), **result}
