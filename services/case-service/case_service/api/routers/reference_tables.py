"""Reference tables API (Form Builder v2 step 3, 2026-10-29).

Admin-managed lists a form reads — branches, products, price bands.

  Design (`design.reference.read` / `design.reference.write`)
    GET    /reference-tables                    the caller's tables + platform ones
    POST   /reference-tables                    create (in the caller's tenant)
    GET    /reference-tables/{table_id}         one table, with its rows (?q= search)
    PATCH  /reference-tables/{table_id}         name, description, columns
    PUT    /reference-tables/{table_id}/rows    replace every row (the grid / a CSV import)
    DELETE /reference-tables/{table_id}         refused while a form uses it

  Run time (any signed-in user of the tenant)
    POST   /reference-tables/options            a SAVED form field's options — never a table
                                                by id: the field names the table, its columns
                                                and filters, so this cannot read any table the
                                                caller likes (as the form lookup, 2026-10-28)

Another tenant's table is 404 at the path guard; a platform table (no tenant)
is read by every tenant and changed only by the platform operator.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import FormDefinitionModel, ReferenceRowModel, ReferenceTableModel
from velaris_shared.db.session import get_session
from velaris_shared.forms import reference as ref
from velaris_shared.tenancy import inherit
from velaris_shared.tenancy.identity import caller_tenant_uuid, is_superadmin

router = APIRouter(prefix="/reference-tables", tags=["reference-tables"], dependencies=[Depends(get_current_user)])


class Column(BaseModel):
    key: str = Field(..., max_length=40)
    label: str = Field("", max_length=100)
    type: str = "text"


class TableIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=2000)
    columns: list[Column] = Field(default_factory=list)


class TablePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    columns: Optional[list[Column]] = None


class RowIn(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class RowsIn(BaseModel):
    rows: list[RowIn] = Field(default_factory=list)


class OptionsIn(BaseModel):
    form_id: uuid.UUID
    field_key: str = Field(..., max_length=255)
    #: The field is a column of this repeating group.
    group_key: Optional[str] = Field(None, max_length=255)
    q: str = Field("", max_length=100)
    #: The form's current values — only the filter fields are read, and they only narrow.
    values: dict[str, Any] = Field(default_factory=dict)


def _columns(cols: list[Column]) -> list[dict[str, str]]:
    if len(cols) > ref.MAX_COLUMNS:
        raise HTTPException(422, f"A table has at most {ref.MAX_COLUMNS} columns.")
    seen: set[str] = set()
    out = []
    for c in cols:
        if not ref.COLUMN_KEY.match(c.key):
            raise HTTPException(422, f"Column key “{c.key}”: lower-case letters, digits and _, starting with a letter.")
        if c.key in seen:
            raise HTTPException(422, f"Column key “{c.key}” is used twice.")
        if c.type not in ref.COLUMN_TYPES:
            raise HTTPException(422, f"Column type must be one of {', '.join(ref.COLUMN_TYPES)}.")
        seen.add(c.key)
        out.append({"key": c.key, "label": c.label or c.key, "type": c.type})
    return out


def _cell(col: dict[str, str], v: Any, row: int) -> Any:
    if v is None or v == "":
        return None
    where = f"Row {row + 1}, {col['label']}"
    t = col["type"]
    if t == "number":
        if isinstance(v, bool):
            raise HTTPException(422, f"{where}: must be a number.")
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{where}: must be a number.") from None
        if n != n or n in (float("inf"), float("-inf")):
            raise HTTPException(422, f"{where}: must be a number.")
        return int(n) if n.is_integer() and abs(n) < 2 ** 53 else n
    if t == "boolean":
        if isinstance(v, bool):
            return v
        if str(v).strip().lower() in ("true", "yes", "1"):
            return True
        if str(v).strip().lower() in ("false", "no", "0"):
            return False
        raise HTTPException(422, f"{where}: must be yes or no.")
    text = str(v)
    if len(text) > ref.MAX_CELL:
        raise HTTPException(422, f"{where}: at most {ref.MAX_CELL} characters.")
    if t == "date":
        import re as _re
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise HTTPException(422, f"{where}: a date like 2026-10-29.")
    return text


def _out(t: ReferenceTableModel) -> dict[str, Any]:
    return {"id": str(t.id), "name": t.name, "description": t.description, "columns": t.columns or [],
            "row_count": t.row_count, "platform": t.tenant_id is None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None}


async def _get(session: AsyncSession, table_id: uuid.UUID) -> ReferenceTableModel:
    t = await session.get(ReferenceTableModel, table_id)
    if t is None:
        raise HTTPException(404, "Reference table not found")
    return t


async def _visible(session: AsyncSession, user: Any):
    """Where-clause: the caller's tables and the platform's — as forms are listed
    (`inherit.scope_list`): only superadmin sees every tenant's."""
    if is_superadmin(user):
        return None
    mine = await caller_tenant_uuid(session, user)
    clause = ReferenceTableModel.tenant_id.is_(None)
    return or_(clause, ReferenceTableModel.tenant_id == mine) if mine else clause


@router.get("")
async def list_tables(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.reference.read")),
):
    stmt = select(ReferenceTableModel).order_by(ReferenceTableModel.name)
    clause = await _visible(session, user)
    if clause is not None:
        stmt = stmt.where(clause)
    return {"items": [_out(t) for t in (await session.execute(stmt)).scalars().all()]}


@router.post("", status_code=201)
async def create_table(
    body: TableIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.reference.write")),
):
    try:
        owner = await inherit.owner_for_create(session, user)
    except inherit.InheritError as e:
        raise HTTPException(e.status, str(e)) from None
    key = inherit.tenant_key_of(owner)
    taken = (await session.execute(select(ReferenceTableModel.id).where(
        ReferenceTableModel.tenant_key == key, ReferenceTableModel.name == body.name.strip()))).first()
    if taken:
        raise HTTPException(409, f"A reference table called “{body.name.strip()}” already exists.")
    t = ReferenceTableModel(tenant_id=owner, tenant_key=key, name=body.name.strip(), description=body.description,
                            columns=_columns(body.columns), row_count=0, created_by=user.user_id)
    session.add(t)
    await session.commit()
    return _out(t)


@router.get("/{table_id}")
async def get_table(
    table_id: uuid.UUID,
    q: str = "",
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.reference.read")),
):
    t = await _get(session, table_id)
    stmt = select(ReferenceRowModel).where(ReferenceRowModel.table_id == t.id).order_by(ReferenceRowModel.sort_order)
    if q.strip():
        stmt = stmt.where(ReferenceRowModel.search_text.contains(q.strip().lower()[:100], autoescape=True))
    rows = (await session.execute(stmt.limit(ref.MAX_ROWS))).scalars().all()
    return {**_out(t), "rows": [{"data": r.data, "is_active": r.is_active} for r in rows]}


@router.patch("/{table_id}")
async def update_table(
    table_id: uuid.UUID,
    body: TablePatch,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.reference.write")),
):
    t = await _get(session, table_id)
    if body.name is not None and body.name.strip() != t.name:
        taken = (await session.execute(select(ReferenceTableModel.id).where(
            ReferenceTableModel.tenant_key == t.tenant_key, ReferenceTableModel.name == body.name.strip()))).first()
        if taken:
            raise HTTPException(409, f"A reference table called “{body.name.strip()}” already exists.")
        t.name = body.name.strip()
    if body.description is not None:
        t.description = body.description
    if body.columns is not None:
        cols = _columns(body.columns)
        # A column a form stores, shows, filters on or fills from cannot go:
        # that form would accept nothing (2026-10-29 audit).
        gone = {c["key"] for c in (t.columns or [])} - {c["key"] for c in cols}
        used = await _forms_using(session, t, gone) if gone else []
        if used:
            raise HTTPException(409, f"Column(s) {', '.join(sorted(gone))} are used by form(s): "
                                     f"{', '.join(used[:5])}. Change those forms first.")
        t.columns = cols
    await session.commit()
    return _out(t)


async def _forms_using(session: AsyncSession, t: ReferenceTableModel, columns: set[str] | None = None) -> list[str]:
    """Names of the forms whose fields read this table (and, given `columns`, one of them)."""
    from velaris_shared.forms.reference import _all_fields
    forms = (await session.execute(select(FormDefinitionModel).where(
        or_(FormDefinitionModel.tenant_id.is_(None), FormDefinitionModel.tenant_id == t.tenant_id)
        if t.tenant_id is not None else true()))).scalars().all()
    out = []
    for f in forms:
        for fld in _all_fields(f.definition_json):
            src = ref.ref_source(fld)
            if not src or str(src.get("table_id")) != str(t.id):
                continue
            if columns is None:
                out.append(f.name); break
            named = {src.get("value_column"), src.get("label_column")} | {
                m.get("column") for m in (src.get("filters") or []) + (src.get("fill") or []) if isinstance(m, dict)}
            if named & columns:
                out.append(f.name); break
    return sorted(set(out))


@router.put("/{table_id}/rows")
async def replace_rows(
    table_id: uuid.UUID,
    body: RowsIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.reference.write")),
):
    t = await _get(session, table_id)
    if len(body.rows) > ref.MAX_ROWS:
        raise HTTPException(422, f"A table has at most {ref.MAX_ROWS} rows.")
    cols = list(t.columns or [])
    clean: list[tuple[dict[str, Any], bool]] = []
    for i, r in enumerate(body.rows):
        unknown = set(r.data) - {c["key"] for c in cols}
        if unknown:
            raise HTTPException(422, f"Row {i + 1}: no column “{sorted(unknown)[0]}”.")
        clean.append(({c["key"]: _cell(c, r.data.get(c["key"]), i) for c in cols}, r.is_active))
    await session.execute(delete(ReferenceRowModel).where(ReferenceRowModel.table_id == t.id))
    session.add_all([ReferenceRowModel(table_id=t.id, sort_order=i, data=data, is_active=active,
                                       search_text=ref.search_text(data))
                     for i, (data, active) in enumerate(clean)])
    t.row_count = len(clean)
    from datetime import datetime, timezone
    t.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return _out(t)


@router.delete("/{table_id}", status_code=204)
async def delete_table(
    table_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.reference.write")),
):
    t = await _get(session, table_id)
    users = await _forms_using(session, t)
    if users:
        raise HTTPException(409, f"Used by form(s): {', '.join(users[:5])}. Remove it there first.")
    await session.delete(t)
    await session.commit()


@router.post("/options")
async def field_options(
    body: OptionsIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """A saved form field's options from its reference table — the only way a
    form reads one at run time. 403 when the field does not take its options
    from a reference table; another tenant's form is 404."""
    form = await session.get(FormDefinitionModel, body.form_id)
    mine = await caller_tenant_uuid(session, user)
    if form is None or (form.tenant_id is not None and form.tenant_id != mine and not is_superadmin(user)):
        raise HTTPException(404, "Form not found")
    field = _field(form.definition_json, body.field_key, body.group_key)
    src = ref.ref_source(field) if field else None
    if src is None:
        raise HTTPException(403, "This field does not take its options from a reference table.")
    rows = (await ref.load_rows(session, {"sections": [{"fields": [field]}]}, mine)).get(str(src.get("table_id") or ""), [])
    values = body.values if isinstance(body.values, dict) else {}
    return {"options": ref.options(ref.matching_rows(rows, src, values.get), src, q=body.q)}


def _field(definition: Any, key: str, group: str | None) -> dict[str, Any] | None:
    from velaris_shared.forms.validate import _fields, field_key_of, group_fields
    for f in _fields(definition):
        if group:
            if field_key_of(f) == group and f.get("type") == "repeating_group":
                return next((c for c in group_fields(f) if field_key_of(c) == key), None)
        elif field_key_of(f) == key:
            return f
    return None
