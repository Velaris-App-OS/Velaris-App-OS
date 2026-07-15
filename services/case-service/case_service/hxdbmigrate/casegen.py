"""HxDBMigrate P3 — generate a draft Velaris case-type from a discovered source table.

The uniquely-BPM step: a table with a status/lifecycle column becomes a draft case-type —
its status values become stages, its columns become case fields. Output is a DRAFT
``definition_json`` (mirrors ``nlp/case_type_builder`` shape); a human reviews and applies
it. Status values used for stages are non-PII workflow states read from a bounded sample;
raw row values are never placed in the definition (enum select-options come only from
low-cardinality columns, and PII columns are high-cardinality → never become selects).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.introspection import get_introspector
from case_service.hxdbmigrate import compliance, semantic

_STATUS_NAMES = ("status", "state", "stage", "phase", "lifecycle", "workflow_state")


def _snake(s: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(s or "")).strip("_").lower()
    return s or "field"


def _field_type(col: dict[str, Any], options: list[str] | None) -> str:
    sem = (col.get("semantic") or {}).get("category")
    t = (col.get("type") or "").lower().split("(")[0].strip()
    if sem == "email":
        return "email"
    if sem == "phone":
        return "phone"
    if sem == "date_of_birth" or t in ("date", "datetime", "timestamp", "timestamptz"):
        return "date"
    if sem == "boolean" or t in ("tinyint", "bool", "boolean", "bit"):
        return "boolean"
    if sem == "enum" and options:
        return "select"
    if sem in ("numeric", "id") or t in ("int", "integer", "bigint", "smallint",
                                          "mediumint", "decimal", "numeric", "float", "double"):
        return "number"
    if sem == "free_text":
        return "textarea"
    return "text"


def detect_workflow_tables(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """From a DEEP analysis schema, return tables that look like workflows.

    A workflow table has a status/lifecycle column (enum semantic or a status-ish name).
    """
    out = []
    for s in schema:
        status = _find_status_column(s.get("columns", []))
        if status:
            out.append({"table": s["table"], "status_column": status,
                        "column_count": len(s.get("columns", [])),
                        "row_estimate": s.get("row_estimate")})
    return out


def _find_status_column(columns: list[dict[str, Any]]) -> str | None:
    # Prefer a status-named column; else any enum-classified column that the
    # compliance scan does NOT flag (a low-cardinality "zip"/"city" column classifies
    # enum but its values are PII — they must never become stage names).
    for c in columns:
        if any(k in c["name"].lower() for k in _STATUS_NAMES):
            return c["name"]
    for c in columns:
        sem = c.get("semantic") or {}
        if sem.get("category") == "enum" and not compliance.scan({"_": {c["name"]: sem}})["findings"]:
            return c["name"]
    return None


def _status_column_problem(status_col: str, columns_by_name: dict[str, dict[str, Any]],
                           distinct: dict[str, list[str]]) -> str | None:
    """Why ``status_col`` must not drive stage generation, or None if it is safe.

    The status column's distinct values are copied verbatim into the definition as
    stage names — so a compliance-flagged column (PII by value or by name hint) or a
    high-cardinality column (stage names would be arbitrary row data) is rejected.
    """
    sc = columns_by_name.get(status_col)
    if sc is None:
        return f"Status column {status_col!r} not found in table"
    sem = sc.get("semantic") or {}
    flagged = compliance.scan({"_": {status_col: sem}})["findings"]
    if flagged:
        return (f"Column {status_col!r} is compliance-flagged as {flagged[0]['sensitivity']} "
                f"— its values cannot be used as workflow stages")
    if len(distinct.get(status_col) or []) > 20:
        return f"Column {status_col!r} has too many distinct values to be a workflow status column"
    return None


async def generate_case_type(session: AsyncSession, table: str,
                             status_column: str | None = None) -> dict[str, Any]:
    """Build a draft case-type definition_json for one source table."""
    insp = get_introspector(session)
    if not await insp.table_exists(session, table):
        raise ValueError(f"Table {table!r} not found in source")

    cols = await insp.columns(session, table)
    columns = [{"name": c["column_name"], "type": c["data_type"], "nullable": c["is_nullable"]}
               for c in cols]

    # One bounded sample → classify + collect distinct values (in memory, non-persisted).
    rows = await semantic.sample_table(session, insp.quote_ident(table))
    distinct: dict[str, list[str]] = {}
    for c in columns:
        vals = [r.get(c["name"]) for r in rows]
        c["semantic"] = semantic.classify_column(c["name"], vals)
        seen = list(dict.fromkeys(str(v) for v in vals if v is not None))
        distinct[c["name"]] = seen

    status_col = status_column or _find_status_column(columns)
    warnings: list[str] = []

    # Guard: stage names are raw distinct values, so the chosen column must be safe.
    if status_col:
        problem = _status_column_problem(status_col, {c["name"]: c for c in columns}, distinct)
        if problem:
            if status_column:   # explicitly requested → hard error
                raise ValueError(problem)
            warnings.append(problem)
            status_col = None   # auto-detected → fall back to the default flow

    # Stages from the status column's real distinct values (non-PII workflow states).
    if status_col and distinct.get(status_col):
        stage_values = distinct[status_col][:20]
    else:
        stage_values = ["Intake", "In Progress", "Done"]
        warnings.append("No status/lifecycle column detected — used a default 3-stage flow.")

    stages = []
    for v in stage_values:
        sid = _snake(v)
        stages.append({
            "id": sid, "name": str(v),
            "steps": [{"id": f"{sid}_step", "name": f"Complete {v}", "fields": []}],
            "sla_hours": None,
        })

    # Fields from columns (enum → select with options; PII columns are high-cardinality → text).
    fields = []
    for c in columns:
        cat = (c.get("semantic") or {}).get("category")
        opts = distinct.get(c["name"]) if cat == "enum" else None
        ftype = _field_type(c, opts)
        fields.append({
            "id": _snake(c["name"]),
            "label": c["name"].replace("_", " ").title(),
            "field_type": ftype,
            "required": c["nullable"] in ("NO", False, "false", 0),
            "options": opts[:20] if (ftype == "select" and opts) else None,
        })

    name = table.replace("_", " ").title().replace(" ", "")
    definition = {"name": name, "stages": stages, "sla_policies": [], "fields": fields}
    rationale = (
        f"Detected {'status column ' + status_col if status_col else 'no status column'} on "
        f"'{table}'. Generated {len(stages)} stage(s) from its values and {len(fields)} "
        f"field(s) from its columns."
    )
    return {"definition_json": definition, "source_table": table,
            "status_column": status_col, "rationale": rationale, "warnings": warnings}
