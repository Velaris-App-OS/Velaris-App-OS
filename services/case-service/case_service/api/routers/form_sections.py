"""Reusable form sections API (Form Builder §5 step 6, 2026-10-29).

An "Address" or "Bank details" section built once and linked from many forms.

  Design (`design.forms.write` — the people who build forms)
    GET    /form-sections                 the caller's sections + platform ones
    POST   /form-sections                 create (in the caller's tenant)
    GET    /form-sections/{section_id}    one section, with the forms that link it
    PUT    /form-sections/{section_id}    change it: version + 1, and every form that
                                          links it gets the new fields in the same
                                          transaction — or nothing changes (409/422)
    DELETE /form-sections/{section_id}    refused while a form links it

Another tenant's section is 404 at the path guard; a platform section (no
tenant) is linked by every tenant and changed only by the platform operator.
A platform form may link only platform sections, so a tenant's section never
reaches another tenant through a default form.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import FormDefinitionModel, FormSectionModel
from velaris_shared.db.session import get_session
from velaris_shared.forms import sections as sec
from velaris_shared.tenancy import inherit
from velaris_shared.tenancy.identity import caller_tenant_uuid, is_superadmin

router = APIRouter(prefix="/form-sections", tags=["form-sections"], dependencies=[Depends(get_current_user)])


class SectionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=2000)
    section: dict[str, Any]


def _out(s: FormSectionModel, used_by: list[str] | None = None) -> dict[str, Any]:
    out = {"id": str(s.id), "name": s.name, "description": s.description, "version": s.version,
           "section": s.section_json or {}, "platform": s.tenant_id is None,
           "updated_at": s.updated_at.isoformat() if s.updated_at else None}
    if used_by is not None:
        out["used_by"] = used_by
    return out


def _body(raw: Any) -> dict[str, Any]:
    try:
        body = sec.check_section_body(raw)
    except sec.SectionError as e:
        raise HTTPException(422, str(e)) from None
    if any(isinstance(f, dict) and f.get("type") == "repeating_group" and any(
            isinstance(c, dict) and c.get("type") == "repeating_group" for c in f.get("fields") or [])
           for f in body["fields"]):
        raise HTTPException(422, "A repeating group cannot hold another one.")
    return body


async def _get(session: AsyncSession, section_id: uuid.UUID) -> FormSectionModel:
    s = await session.get(FormSectionModel, section_id)
    if s is None:
        raise HTTPException(404, "Section not found")
    return s


async def _linking_forms(session: AsyncSession, s: FormSectionModel) -> list[FormDefinitionModel]:
    """The forms that link this section (a tenant's section: that tenant's forms)."""
    stmt = select(FormDefinitionModel).where(
        FormDefinitionModel.tenant_id == s.tenant_id if s.tenant_id is not None else true())
    forms = (await session.execute(stmt)).scalars().all()
    out = []
    for f in forms:
        try:
            if s.id in sec.linked_ids(f.definition_json):
                out.append(f)
        except sec.SectionError:
            continue
    return out


@router.get("")
async def list_sections(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    stmt = select(FormSectionModel).order_by(FormSectionModel.name)
    if not is_superadmin(user):
        mine = await caller_tenant_uuid(session, user)
        stmt = stmt.where(FormSectionModel.tenant_id.is_(None) | (FormSectionModel.tenant_id == mine)) if mine \
            else stmt.where(FormSectionModel.tenant_id.is_(None))
    return {"items": [_out(s) for s in (await session.execute(stmt)).scalars().all()]}


@router.post("", status_code=201)
async def create_section(
    body: SectionIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    try:
        owner = await inherit.owner_for_create(session, user)
    except inherit.InheritError as e:
        raise HTTPException(e.status, str(e)) from None
    key = inherit.tenant_key_of(owner)
    name = body.name.strip()
    if (await session.execute(select(FormSectionModel.id).where(
            FormSectionModel.tenant_key == key, FormSectionModel.name == name))).first():
        raise HTTPException(409, f"A section called “{name}” already exists.")
    content = _body(body.section)
    from case_service.api.routers.forms import _check_embeds
    await _check_embeds(session, owner or await caller_tenant_uuid(session, user), {"sections": [content]})
    s = FormSectionModel(id=uuid.uuid4(), tenant_id=owner, tenant_key=key, name=name, description=body.description,
                         version=1, section_json=content, updated_by=user.user_id)
    session.add(s)
    await session.commit()
    return _out(s, [])


@router.get("/{section_id}")
async def get_section(
    section_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    s = await _get(session, section_id)
    return _out(s, sorted({f.name for f in await _linking_forms(session, s)}))


@router.put("/{section_id}")
async def update_section(
    section_id: uuid.UUID,
    body: SectionIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    """Change the section and every form that links it, together. A form the
    change would break (a field key clash, an embed its tenant has not
    approved) stops the whole change, naming the form."""
    from case_service.api.routers.forms import _check_embeds
    s = await _get(session, section_id)
    name = body.name.strip()
    if name != s.name and (await session.execute(select(FormSectionModel.id).where(
            FormSectionModel.tenant_key == s.tenant_key, FormSectionModel.name == name))).first():
        raise HTTPException(409, f"A section called “{name}” already exists.")
    content = _body(body.section)
    s.name, s.description, s.section_json, s.version = name, body.description, content, s.version + 1
    s.updated_by = user.user_id
    updated = []
    for f in await _linking_forms(session, s):
        form_name = f.name                       # read before a rollback expires it
        try:
            f.definition_json = sec.materialize(f.definition_json, {s.id: s, **await _sources_for(session, f, skip=s.id)})
        except sec.SectionError as e:
            await session.rollback()
            raise HTTPException(409, f"Form “{form_name}”: {e}") from None
        try:
            await _check_embeds(session, f.tenant_id or await caller_tenant_uuid(session, user), f.definition_json)
        except HTTPException as e:
            await session.rollback()
            raise HTTPException(422, f"Form “{form_name}”: {e.detail}") from None
        updated.append(form_name)
    await session.commit()
    return {**_out(s), "updated_forms": sorted(set(updated))}


async def _sources_for(session: AsyncSession, form: FormDefinitionModel, skip: uuid.UUID) -> dict[uuid.UUID, Any]:
    """The other sections a form links, as they are now (so a re-copy keeps them)."""
    ids = sec.linked_ids(form.definition_json) - {skip}
    if not ids:
        return {}
    return {r.id: r for r in (await session.execute(
        select(FormSectionModel).where(FormSectionModel.id.in_(ids)))).scalars().all()}


@router.delete("/{section_id}", status_code=204)
async def delete_section(
    section_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    s = await _get(session, section_id)
    used = sorted({f.name for f in await _linking_forms(session, s)})
    if used:
        raise HTTPException(409, f"Linked by form(s): {', '.join(used[:5])}. Unlink it there first.")
    await session.delete(s)
    await session.commit()
