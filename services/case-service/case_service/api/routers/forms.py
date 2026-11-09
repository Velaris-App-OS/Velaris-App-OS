"""Forms API router.

CRUD for form definitions.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.forms import (
    FormCreate,
    FormListResponse,
    FormResponse,
    FormUpdate,
)
from velaris_shared.db import repository as repo
from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.session import get_session
from velaris_shared.db.models import FormDefinitionModel
from velaris_shared.forms import embed as _embed
from velaris_shared.tenancy import inherit
from velaris_shared.tenancy.identity import caller_tenant_uuid

router = APIRouter(prefix="/forms", tags=["forms"], dependencies=[Depends(get_current_user)])


@router.post("", response_model=FormResponse, status_code=201)
async def create_form(
    body: FormCreate,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    # Validate data model reference if provided
    if body.data_model_id:
        dm = await repo.get_data_model(session, body.data_model_id)
        if dm is None:
            raise HTTPException(
                status_code=404, detail="Data model not found"
            )

    # 8e: a tenant's form is its own; only the platform operator makes a
    # default (as before). Unique per tenant since migration 214.
    owner = await _inherit(inherit.owner_for_create(session, _user))
    body.definition_json = await _link_sections(session, owner, body.definition_json)
    await _check_embeds(session, owner or await caller_tenant_uuid(session, _user), body.definition_json)
    if await inherit.name_taken(session, FormDefinitionModel, owner,
                                name=body.name, version=body.version):
        raise HTTPException(409, f"Form '{body.name}' v{body.version} already exists")

    form = await repo.create_form(
        session,
        data={
            "tenant_id": owner,
            "name": body.name,
            "version": body.version,
            "data_model_id": body.data_model_id,
            "definition_json": body.definition_json,
        },
    )
    return form


@router.get("", response_model=FormListResponse)
async def list_forms(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # 8e: the caller's own forms and the defaults it has not forked.
    from sqlalchemy import select as _select
    clause = (await inherit.scope_list(_select(FormDefinitionModel), FormDefinitionModel,
                                       session, user)).whereclause
    items, total = await repo.list_forms(
        session,
        offset=(page - 1) * page_size,
        limit=page_size,
        where=clause,
    )
    await inherit.flag_updates(session, FormDefinitionModel, items)   # 8k
    return FormListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


# ── Embed field: the tenant's approved sites (2026-10-29) ─────────────
# Before `/{form_id}`, so this path is not read as a form id.

class EmbedSettingsIn(BaseModel):
    allowed_origins: list[str] = Field(default_factory=list, max_length=_embed.MAX_ORIGINS)
    portal: bool = False


async def _tenant_row(session: AsyncSession, tenant_id: Any):
    from velaris_shared.db.models import TenantModel
    return await session.get(TenantModel, tenant_id) if tenant_id else None


async def _link_sections(session: AsyncSession, owner: Any, definition: Any) -> Any:
    """Copy every linked reusable section's fields into the form (§5 step 6).
    A form may link its own tenant's sections and platform ones; a platform
    form only platform ones, so a tenant's section never reaches another
    tenant. Whatever the browser sent for linked fields is replaced."""
    from sqlalchemy import or_, select
    from velaris_shared.db.models import FormSectionModel
    from velaris_shared.forms import sections as _sec
    try:
        ids = _sec.linked_ids(definition)
        if not ids:
            return definition          # no link: saved exactly as before
        mine = FormSectionModel.tenant_id.is_(None)
        if owner is not None:
            mine = or_(mine, FormSectionModel.tenant_id == owner)
        rows = (await session.execute(select(FormSectionModel).where(FormSectionModel.id.in_(ids), mine))).scalars().all()
        return _sec.materialize(definition, {r.id: r for r in rows})
    except _sec.SectionError as e:
        raise HTTPException(422, str(e)) from None


async def _check_embeds(session: AsyncSession, tenant_id: Any, definition: Any) -> None:
    """A form naming an embed its owner's effective list does not approve is not
    saved (`velaris_shared.forms.embed.form_embed_problem` — every form-writing
    path asks the same function)."""
    problem = await _embed.form_embed_problem(session, tenant_id, definition)
    if problem:
        raise HTTPException(422, problem)


@router.get("/embed-settings")
async def get_embed_settings(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The sites the caller's tenant's forms may show — its own list and the
    platform's, minus the blocked (2026-11-04) — and whether typing into them is
    allowed. Every form renderer needs them; nothing secret."""
    tenant = await _tenant_row(session, await caller_tenant_uuid(session, user))
    return await _embed.effective_for(session, tenant)


@router.put("/embed-settings")
async def put_embed_settings(
    body: EmbedSettingsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.embeds.manage")),
):
    """Replace the caller's tenant's approved sites. Each entry is reduced to
    its origin; only https with a real host name, never Studio's own origin
    (a page from there could reach the signed-in session)."""
    origins, refused = _embed.normalise_origins(body.allowed_origins)
    if refused:
        raise HTTPException(422, f"Not an https site: {', '.join(refused[:5])}")
    own = _embed.origin_of(request.headers.get("origin") or "")
    if own and own in origins:
        raise HTTPException(422, f"{own} is this platform's own address and cannot be embedded.")
    tenant = await _tenant_row(session, await caller_tenant_uuid(session, user))
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    # The operator's policy (2026-11-04): may this tenant keep its own list,
    # and is any site on it blocked platform-wide?
    eff = await _embed.effective_for(session, tenant)
    if origins and not eff["may_add"]:
        raise HTTPException(403, "This platform's operator approves the sites forms may embed; "
                                 "a tenant cannot add its own.")
    blocked = [o for o in origins if o in eff["blocked"]]
    if blocked:
        raise HTTPException(422, f"Blocked on this platform: {', '.join(blocked)}")
    before = _embed.settings_of(tenant.settings)
    settings = dict(tenant.settings or {})
    settings[_embed.SETTINGS_KEY] = {"allowed_origins": origins, "portal": body.portal}
    tenant.settings = settings
    await session.flush()
    # On the record (2026-11-04): an approved site is shown inside forms.
    from velaris_shared.db.security_events import log_security_event
    await log_security_event(
        session, event_type="allowed_sites_changed", severity="high",
        user_id=getattr(user, "user_id", None), resource_type="tenant", resource_id=str(tenant.id),
        action="update", outcome="success",
        details={"scope": "tenant", "added": sorted(set(origins) - set(before["allowed_origins"])),
                 "removed": sorted(set(before["allowed_origins"]) - set(origins)),
                 "portal": body.portal})
    return await _embed.effective_for(session, tenant)


# ── Allowed sites: the platform operator's policy (2026-11-04) ────────

class AllowedSitesPolicyIn(BaseModel):
    platform_origins: list[str] = Field(default_factory=list, max_length=_embed.MAX_ORIGINS)
    blocked: list[str] = Field(default_factory=list, max_length=_embed.MAX_ORIGINS)
    tenants_may_add: bool = True
    allow_forms: bool = True


async def _operator_only(session: AsyncSession, user: AuthenticatedUser) -> None:
    from velaris_shared.tenancy.identity import is_platform_operator
    if not await is_platform_operator(session, user):
        raise HTTPException(403, "Platform operator only.")


@router.get("/allowed-sites/policy")
async def get_allowed_sites_policy(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.embeds.manage")),
):
    """The operator's embed policy. Read by the operator only: what other
    tenants may do is the install's business."""
    await _operator_only(session, user)
    return await _embed.load_policy(session)


@router.put("/allowed-sites/policy")
async def put_allowed_sites_policy(
    body: AllowedSitesPolicyIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.embeds.manage")),
):
    """Replace the operator's embed policy (kept with the default tenant)."""
    await _operator_only(session, user)
    from velaris_shared.tenancy.identity import DEFAULT_TENANT, resolve_tenant
    plat, bad1 = _embed.normalise_origins(body.platform_origins)
    blocked, bad2 = _embed.normalise_origins(body.blocked)
    if bad1 or bad2:
        raise HTTPException(422, f"Not an https site: {', '.join((bad1 + bad2)[:5])}")
    own = _embed.origin_of(request.headers.get("origin") or "")
    if own and own in plat:
        raise HTTPException(422, f"{own} is this platform's own address and cannot be embedded.")
    clash = sorted(set(plat) & set(blocked))
    if clash:
        raise HTTPException(422, f"Both approved and blocked: {', '.join(clash)}")
    hit = await resolve_tenant(session, DEFAULT_TENANT)
    row = await _tenant_row(session, hit[0]) if hit else None
    if row is None:
        raise HTTPException(404, "The default tenant is missing")
    before = _embed.policy_of(row.settings)
    settings = dict(row.settings or {})
    root = dict(settings.get(_embed.POLICY_KEY) or {}) if isinstance(settings.get(_embed.POLICY_KEY), dict) else {}
    root["embeds"] = {"platform_origins": plat, "blocked": blocked,
                      "tenants_may_add": body.tenants_may_add, "allow_forms": body.allow_forms}
    settings[_embed.POLICY_KEY] = root
    row.settings = settings
    await session.flush()
    from velaris_shared.db.security_events import log_security_event
    after = _embed.policy_of(settings)
    await log_security_event(
        session, event_type="allowed_sites_changed", severity="high",
        user_id=getattr(user, "user_id", None), resource_type="platform", resource_id="allowed_sites",
        action="update", outcome="success",
        details={"scope": "platform", "before": before, "after": after})
    return after


# ── Pickers: what a user / case / document picker offers (batch M, 2026-10-29) ──

class PickerOptionsIn(BaseModel):
    form_id: uuid.UUID
    field_key: str = Field(..., max_length=255)
    #: The field is a column of this repeating group.
    group_key: str | None = Field(None, max_length=255)
    q: str = Field("", max_length=100)
    #: The case the form is being filled for — a document picker lists only its documents.
    case_id: uuid.UUID | None = None


@router.post("/picker-options")
async def picker_options(
    body: PickerOptionsIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """A saved form's picker field searched — never a free directory query: the
    form must be one the caller may read and the field a picker of it. The
    rules are the ones submit applies (`velaris_shared.forms.pickers`)."""
    from velaris_shared.forms.pickers import picker_options as _options
    from velaris_shared.forms.validate import PICKER_KINDS, field_key_of
    from velaris_shared.tenancy.identity import case_visible_to, is_superadmin, same_tenant
    form = await repo.get_form(session, body.form_id)
    if form is None or (form.tenant_id is not None
                        and not await same_tenant(session, form.tenant_id, getattr(user, "tenant_id", None))):
        raise HTTPException(404, "Form not found")
    fields: list = []
    for sec in (form.definition_json or {}).get("sections") or []:
        fields += [f for f in sec.get("fields") or [] if isinstance(f, dict)]
    if body.group_key:
        group = next((f for f in fields if field_key_of(f) == body.group_key and f.get("type") == "repeating_group"), None)
        fields = [f for f in (group or {}).get("fields") or [] if isinstance(f, dict)]
    field = next((f for f in fields if field_key_of(f) == body.field_key and f.get("type") in PICKER_KINDS), None)
    if field is None:
        raise HTTPException(404, "No such picker on this form")
    kind = PICKER_KINDS[field["type"]]
    if kind == "document":
        from velaris_shared.db.models import CaseInstanceModel
        case = await session.get(CaseInstanceModel, body.case_id) if body.case_id else None
        if case is None or not await case_visible_to(session, case.tenant_id, getattr(user, "tenant_id", None),
                                                     is_superadmin_caller=is_superadmin(user)):
            raise HTTPException(404, "Case not found")
    return {"options": await _options(session, kind, body.q, user=user, case_id=body.case_id)}


# ── Field-level security + test scenarios (§5 step 7, 2026-10-29) ─────────────

@router.get("/access-roles")
async def form_access_roles(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    """The access roles a field can be limited to: the caller's tenant's and the
    platform's — names and ids only (the full role is `identity.roles.read`)."""
    from sqlalchemy import or_, select
    from velaris_shared.db.models import AccessRoleModel
    from velaris_shared.tenancy.identity import is_superadmin
    stmt = select(AccessRoleModel.id, AccessRoleModel.name).order_by(AccessRoleModel.name)
    if not is_superadmin(user):
        tid = getattr(user, "tenant_id", None)
        stmt = stmt.where(or_(AccessRoleModel.tenant_id.is_(None),
                              *( [AccessRoleModel.tenant_id == str(tid)] if tid else [] )))
    rows = (await session.execute(stmt)).all()
    return {"roles": [{"id": str(r.id), "name": r.name} for r in rows]}


#: A form's scenarios are a check before publishing, not a load test.
TESTS_MAX = 50


class FormTestsIn(BaseModel):
    definition: dict[str, Any]
    tests: list[dict[str, Any]] = Field(default_factory=list, max_length=TESTS_MAX)


@router.post("/test-run")
async def run_form_tests(
    body: FormTestsIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    """Run a form's test scenarios through the server's own check — the one a
    submission meets — on the definition as it is in the builder (saved or
    not). Nothing is stored and no connector is called; reference tables are
    the caller's tenant's. Pickers are not checked (they need a case)."""
    from velaris_shared.forms import validate as validate_form
    from velaris_shared.forms.reference import load_rows, table_ids
    from velaris_shared.forms.security import secure_definition
    from velaris_shared.forms.testing import judge
    definition = body.definition
    refs = {}
    if table_ids(definition):
        try:
            async with session.begin_nested():
                refs = await load_rows(session, definition, await caller_tenant_uuid(session, user))
        except Exception:                               # noqa: BLE001
            refs = {t: [] for t in table_ids(definition)}
    results = []
    for t in body.tests:
        values = t.get("values") if isinstance(t.get("values"), dict) else {}
        roles = t.get("roles")
        d = secure_definition(definition, {str(r) for r in roles}, for_browser=False) \
            if isinstance(roles, list) and roles else definition
        try:
            clean, errors = validate_form(d, values, context={}, references=refs)
        except Exception as exc:                        # noqa: BLE001 — a scenario never fails the run
            results.append({"name": t.get("name") or "", "passed": False,
                            "problems": [f"The check could not run: {type(exc).__name__}"],
                            "errors": [], "values": {}})
            continue
        results.append({"name": t.get("name") or "", **judge(t.get("expect"), clean, errors)})
    return {"results": results, "passed": sum(1 for r in results if r["passed"]), "total": len(results)}


@router.get("/{form_id}", response_model=FormResponse)
async def get_form(
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    form = await repo.get_form(session, form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    # 8e: case types name forms by id; a tenant that forked this default gets
    # its fork. (Another tenant's form is 404 at the path guard.)
    form = await inherit.resolve_for_caller(session, user, FormDefinitionModel, form)
    return (await inherit.flag_updates(session, FormDefinitionModel, [form]))[0]   # 8k


@router.patch("/{form_id}", response_model=FormResponse)
async def update_form(
    form_id: uuid.UUID,
    body: FormUpdate,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    form = await repo.get_form(session, form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    # 8e: a tenant's edit of a default lands in its fork.
    form = await _inherit(inherit.target_for_write(session, _user, FormDefinitionModel, form))
    form_id = form.id

    values = {}
    if body.data_model_id is not None:
        values["data_model_id"] = body.data_model_id
    if body.definition_json is not None:
        body.definition_json = await _link_sections(session, form.tenant_id, body.definition_json)
        await _check_embeds(session, form.tenant_id or await caller_tenant_uuid(session, _user), body.definition_json)
        values["definition_json"] = body.definition_json
    if body.version is not None:
        values["version"] = body.version

    if values:
        await repo.update_form(session, form_id, values=values)

    return await repo.get_form(session, form_id)


@router.delete("/{form_id}", status_code=204)
async def delete_form(
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    form = await repo.get_form(session, form_id)
    if form is not None:
        await _inherit(inherit.assert_may_delete(session, _user, form))
    deleted = await repo.delete_form(session, form_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Form not found")


async def _inherit(awaitable):
    """Run an `inherit` step, answering its refusal as HTTP."""
    try:
        return await awaitable
    except inherit.InheritError as exc:
        raise HTTPException(exc.status, str(exc))


@router.get("/{form_id}/variables")
async def form_variables(
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    """The case variables a field of this form can be pre-filled from.

    Form Builder part 6 (2026-10-28): the designer picks from a list instead
    of typing `crm.account_status`. A form is used by the steps that name it;
    their case types (the caller's tenant's, and platform-shared ones) define
    the variables. A form no step uses has none to offer yet.
    """
    from sqlalchemy import select

    from case_service.tenancy.identity import scope_to_caller
    from velaris_shared.db.models import (
        CaseTypeModel, CaseTypeVariableModel, VariableNamespaceModel,
    )

    if await repo.get_form(session, form_id) is None:
        raise HTTPException(status_code=404, detail="Form not found")
    stmt = await scope_to_caller(select(CaseTypeModel), CaseTypeModel, session, user)
    users: list[CaseTypeModel] = []
    for ct in (await session.execute(stmt)).scalars():
        steps = (st for sg in (ct.definition_json or {}).get("stages", []) or []
                 for st in sg.get("steps", []) or [])
        if any(str(st.get("form_id") or "") == str(form_id) for st in steps):
            users.append(ct)
    variables: dict[str, dict] = {}
    if users:
        rows = (await session.execute(
            select(CaseTypeVariableModel, VariableNamespaceModel.name)
            .join(VariableNamespaceModel, CaseTypeVariableModel.namespace_id == VariableNamespaceModel.id)
            .where(CaseTypeVariableModel.case_type_id.in_([ct.id for ct in users]))
            .order_by(CaseTypeVariableModel.full_key)
        )).all()
        names = {ct.id: ct.name for ct in users}
        for d, ns in rows:
            v = variables.setdefault(d.full_key, {
                "full_key": d.full_key, "namespace": ns, "label": d.label,
                "var_type": d.var_type, "case_types": []})
            v["case_types"].append(names.get(d.case_type_id))
    return {
        "case_types": [{"id": str(ct.id), "name": ct.name} for ct in users],
        "variables": list(variables.values()),
    }

