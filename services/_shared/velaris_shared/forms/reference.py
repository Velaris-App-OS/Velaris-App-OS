"""Reference tables in forms (Form Builder v2 step 3, 2026-10-29).

A choice field (dropdown, radio, multi-select) may take its options from a
reference table:

    "options_source": {"kind": "reference", "table_id": "<uuid>",
                       "value_column": "code", "label_column": "name",
                       "filters": [{"column": "region", "field": "region"}],   # cascading
                       "fill":    [{"column": "price", "field": "unit_price"}]}

* Options are only those rows whose filter columns equal the named fields'
  current values — a child list follows its parent.
* On submit the server checks every chosen value is one of those rows
  (`validate(..., references=load_rows(...))`); the browser's list is never
  trusted.
* `fill` copies columns of the chosen row into other fields. A target the
  person cannot change (read-only) holds the table's value on the server,
  never the one sent.

Used by case-service (staff forms) and portal-service (customer forms); both
load the rows with `load_rows`, which only reads tables the form's tenant may.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Callable, Iterator

MAX_COLUMNS = 20
MAX_ROWS = 10_000
MAX_CELL = 1000
MAX_OPTIONS = 50
COLUMN_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
COLUMN_TYPES = ("text", "number", "date", "boolean")


def ref_source(field: dict[str, Any]) -> dict[str, Any] | None:
    """The field's reference source, or None. One without a table yet is still
    a reference source: it offers nothing and accepts nothing (2026-10-29 audit —
    it accepted anything)."""
    src = field.get("options_source")
    if isinstance(src, dict) and src.get("kind") == "reference":
        return src
    return None


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _all_fields(definition: Any) -> Iterator[dict[str, Any]]:
    sections = definition.get("sections") if isinstance(definition, dict) else None
    for sec in sections if isinstance(sections, list) else []:
        for f in (sec.get("fields") if isinstance(sec, dict) and isinstance(sec.get("fields"), list) else []):
            if not isinstance(f, dict):
                continue
            yield f
            if f.get("type") == "repeating_group" and isinstance(f.get("fields"), list):
                yield from (c for c in f["fields"] if isinstance(c, dict))


def table_ids(definition: Any) -> set[str]:
    """Every reference table the form's fields read."""
    out: set[str] = set()
    for f in _all_fields(definition):
        src = ref_source(f)
        if src and src.get("table_id"):
            out.add(str(src["table_id"]))
    return out


def matching_rows(rows: list[dict[str, Any]], src: dict[str, Any], reads: Callable[[str], Any]) -> list[dict[str, Any]]:
    """The rows a field may choose from now: every filter column equals its field's value.
    A filter whose field is empty allows nothing — the child list waits for its parent."""
    filters = [f for f in (src.get("filters") if isinstance(src.get("filters"), list) else [])
               if isinstance(f, dict) and f.get("column") and f.get("field")]
    if not filters:
        return rows
    wanted = []
    for f in filters:
        v = reads(str(f["field"]))
        if v is None or v == "" or v == []:
            return []
        wanted.append((str(f["column"]), _text(v)))
    return [r for r in rows if all(_text(r.get(c)) == v for c, v in wanted)]


def row_for(rows: list[dict[str, Any]], src: dict[str, Any], value: Any) -> dict[str, Any] | None:
    col = str(src.get("value_column") or "")
    want = _text(value)
    return next((r for r in rows if _text(r.get(col)) == want), None) if col else None


def options(rows: list[dict[str, Any]], src: dict[str, Any], *, q: str = "", limit: int = MAX_OPTIONS) -> list[dict[str, Any]]:
    """`[{label, value, row}]` for the browser — `row` holds only the columns the field fills from."""
    vcol, lcol = str(src.get("value_column") or ""), str(src.get("label_column") or src.get("value_column") or "")
    fill_cols = {str(f["column"]) for f in (src.get("fill") if isinstance(src.get("fill"), list) else [])
                 if isinstance(f, dict) and f.get("column")}
    needle = q.strip().lower()
    out = []
    for r in rows:
        label = _text(r.get(lcol))
        if needle and needle not in label.lower() and needle not in _text(r.get(vcol)).lower():
            continue
        out.append({"label": label, "value": r.get(vcol),
                    **({"row": {c: r.get(c) for c in fill_cols}} if fill_cols else {})})
        if len(out) >= limit:
            break
    return out


def search_text(data: dict[str, Any]) -> str:
    return " ".join(_text(v) for v in data.values()).lower()[:2000]


async def load_rows(session: Any, definition: Any, tenant_id: Any) -> dict[str, list[dict[str, Any]]]:
    """`{table_id: [row data, …]}` for every table the form reads — active rows,
    in order. A table of another tenant, or one that is gone, loads as no rows
    (so nothing chosen from it is accepted)."""
    from sqlalchemy import or_, select
    from velaris_shared.db.models import ReferenceRowModel, ReferenceTableModel

    ids = []
    for t in table_ids(definition):
        try:
            ids.append(uuid.UUID(t))
        except ValueError:
            continue
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in table_ids(definition)}
    if not ids:
        return out
    owner = ReferenceTableModel.tenant_id.is_(None)
    if tenant_id is not None:
        owner = or_(owner, ReferenceTableModel.tenant_id == tenant_id)
    allowed = (await session.execute(
        select(ReferenceTableModel.id).where(ReferenceTableModel.id.in_(ids), owner))).scalars().all()
    if not allowed:
        return out
    rows = (await session.execute(
        select(ReferenceRowModel.table_id, ReferenceRowModel.data)
        .where(ReferenceRowModel.table_id.in_(allowed), ReferenceRowModel.is_active.is_(True))
        .order_by(ReferenceRowModel.table_id, ReferenceRowModel.sort_order))).all()
    for tid, data in rows:
        out.setdefault(str(tid), []).append(data if isinstance(data, dict) else {})
    return out
