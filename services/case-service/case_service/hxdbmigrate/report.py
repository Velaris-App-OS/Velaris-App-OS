"""HxDBMigrate — discovery report: Schema Autobiography + data-quality scoring (P1).

Deterministic and read-light: it reuses the DB-SDK introspection layer against the source
session (structural metadata only — no per-row scans), so it works on any-size source with
a bounded number of catalog queries. An AI narrative over this report is a later, optional,
egress-gated add-on (see docs/Future/HxDBMigrate.md).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.introspection import get_introspector


def _jsonable(obj: Any) -> Any:
    """Coerce introspection values to JSON-safe types (the report is persisted as JSONB).

    information_schema returns some numerics as Decimal and some catalogs return datetimes;
    neither is JSON-serializable by default, so normalise before persist/return.
    """
    if isinstance(obj, Decimal):
        i = int(obj)
        return i if obj == i else float(obj)
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


async def analyze_source(session: AsyncSession, deep: bool = False) -> dict[str, Any]:
    """Introspect the source and build the discovery report.

    Structural always: {table_count, quality, autobiography, schema}.
    ``deep=True`` (P2) additionally samples values to add per-column ``semantic`` +
    ``mapping`` hints and a top-level ``compliance`` (PII/PHI) scan + ``pii_count``.
    """
    insp = get_introspector(session)
    tables = await insp.list_tables(session)
    table_names = {t["table_name"] for t in tables}

    schema: list[dict[str, Any]] = []
    for t in tables:
        name = t["table_name"]
        cols = await insp.columns(session, name)
        idx = await insp.indexes(session, name)
        fks = await insp.foreign_keys(session, name)
        schema.append({
            "table": name,
            "row_estimate": t.get("row_estimate"),
            "total_size": t.get("total_size"),
            "columns": [
                {"name": c["column_name"], "type": c["data_type"], "nullable": c["is_nullable"]}
                for c in cols
            ],
            "indexes": idx,
            "foreign_keys": fks,
            "has_primary_key": any(i.get("is_primary") for i in idx),
        })

    report: dict[str, Any] = {
        "table_count": len(schema),
        "quality": _score_quality(schema, table_names),
        "autobiography": _autobiography(schema),
        "schema": schema,
        "deep": deep,
    }

    if deep:
        report.update(await _deep_analyze(session, insp, schema))

    # JSON-safe: the report is stored as JSONB and returned as API JSON.
    return _jsonable(report)


async def _deep_analyze(session: AsyncSession, insp, schema: list[dict[str, Any]]) -> dict[str, Any]:
    """Sample values → semantic classification + type mapping (in place) + compliance scan."""
    from case_service.hxdbmigrate import compliance, mapping, semantic

    semantic_by_table: dict[str, dict[str, dict[str, Any]]] = {}
    for s in schema[: semantic.MAX_SAMPLE_TABLES]:
        table = s["table"]
        col_names = [c["name"] for c in s["columns"]]
        # Always add deterministic type-mapping (no data read needed).
        for c in s["columns"]:
            c["mapping"] = mapping.map_type(c["type"])
        try:
            rows = await semantic.sample_table(session, insp.quote_ident(table))
        except Exception:
            continue  # a table we can't sample (permissions/oddity) is skipped, not fatal
        per_col: dict[str, dict[str, Any]] = {}
        for c in s["columns"]:
            values = [r.get(c["name"]) for r in rows]
            cls = semantic.classify_column(c["name"], values)
            c["semantic"] = cls
            per_col[c["name"]] = cls
        semantic_by_table[table] = per_col

    comp = compliance.scan(semantic_by_table)
    return {"compliance": comp, "pii_count": comp["summary"]["pii_column_count"]}


def _score_quality(schema: list[dict[str, Any]], table_names: set[str]) -> dict[str, Any]:
    """Structural data-quality score (0–100) from primary keys, orphan FKs, and indexing."""
    no_pk = [s["table"] for s in schema if not s["has_primary_key"]]
    no_index = [s["table"] for s in schema if not s["indexes"]]
    orphan_fks = [
        {"table": s["table"], "column": fk.get("column_name"), "references": fk.get("foreign_table")}
        for s in schema for fk in s["foreign_keys"]
        if fk.get("foreign_table") and fk["foreign_table"] not in table_names
    ]

    n = max(len(schema), 1)
    score = 100
    score -= min(40, round(40 * len(no_pk) / n))       # missing PKs are the biggest smell
    score -= min(30, 5 * len(orphan_fks))              # dangling references
    score -= min(15, round(15 * len(no_index) / n))    # unindexed tables
    score = max(0, score)

    findings: list[dict[str, Any]] = []
    if no_pk:
        findings.append({"severity": "high",
                         "issue": f"{len(no_pk)} table(s) without a primary key",
                         "tables": no_pk[:20]})
    if orphan_fks:
        findings.append({"severity": "high",
                         "issue": f"{len(orphan_fks)} foreign key(s) reference a missing table",
                         "details": orphan_fks[:20]})
    if no_index:
        findings.append({"severity": "medium",
                         "issue": f"{len(no_index)} table(s) with no indexes",
                         "tables": no_index[:20]})
    return {"score": score, "findings": findings}


def _autobiography(schema: list[dict[str, Any]]) -> str:
    """Human-readable Markdown summary, largest tables first."""
    lines = ["# Schema Autobiography", "",
             f"This database has **{len(schema)} table(s)**.", ""]
    for s in sorted(schema, key=lambda x: -(x["row_estimate"] or 0))[:100]:
        cols = ", ".join(f"{c['name']} ({c['type']})" for c in s["columns"][:12])
        more = "…" if len(s["columns"]) > 12 else ""
        refs = sorted({fk["foreign_table"] for fk in s["foreign_keys"] if fk.get("foreign_table")})
        rel = f" — references {', '.join(refs)}" if refs else ""
        lines += [
            f"## {s['table']}  (~{s['row_estimate'] or 0} rows)",
            f"- Columns: {cols}{more}",
            f"- Primary key: {'yes' if s['has_primary_key'] else 'NONE'}{rel}",
            "",
        ]
    return "\n".join(lines)
