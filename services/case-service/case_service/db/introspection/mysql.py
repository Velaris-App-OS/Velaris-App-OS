"""MySQL implementation of the HxDBManager introspection contract.

Maps each Postgres primitive to its MySQL equivalent (see ``postgres.py`` for the PG
side and ``base.py`` for the return-shape contract). Key differences handled here:

  * schema scope:    Postgres ``table_schema='public'``  -> MySQL ``table_schema=DATABASE()``
                     (MySQL has no "public" schema; the active database IS the schema).
                     This is the silent-empty-result landmine — get it wrong and every
                     query returns zero rows with no error.
  * sizes:           ``pg_size_pretty(pg_total_relation_size(...))`` has no MySQL
                     equivalent -> human string built from data_length+index_length.
  * indexes:         ``pg_index`` -> ``information_schema.STATISTICS`` (no per-index size
                     or scan counts -> returned as None).
  * EXPLAIN:         ``EXPLAIN (FORMAT JSON)`` -> ``EXPLAIN FORMAT=JSON`` (and MySQL hands
                     back a JSON *string*, so we parse it to match PG's decoded shape).
  * statement timeout: ``SET LOCAL statement_timeout`` -> ``SET SESSION max_execution_time``
                     (milliseconds; applies to SELECT only — the value IS the intended
                     safety ceiling so persisting it on the connection is harmless).
  * slow queries:    ``pg_stat_statements`` is PG-only -> reported unavailable (degraded
                     by design; reimplementing on performance_schema is out of scope).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .base import DbIntrospector

# Human-readable byte size, MySQL's stand-in for pg_size_pretty().
_SIZE_EXPR = """COALESCE(CASE
    WHEN (t.data_length + t.index_length) >= 1073741824
        THEN CONCAT(ROUND((t.data_length + t.index_length) / 1073741824, 2), ' GB')
    WHEN (t.data_length + t.index_length) >= 1048576
        THEN CONCAT(ROUND((t.data_length + t.index_length) / 1048576, 2), ' MB')
    WHEN (t.data_length + t.index_length) >= 1024
        THEN CONCAT(ROUND((t.data_length + t.index_length) / 1024, 2), ' kB')
    ELSE CONCAT(t.data_length + t.index_length, ' bytes')
END, '?')"""


class MysqlIntrospector(DbIntrospector):
    name = "mysql"

    def quote_ident(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    async def table_exists(self, session: AsyncSession, table: str) -> bool:
        row = await session.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name=:t"
        ), {"t": table})
        return row.scalar_one_or_none() is not None

    async def column_names(self, session: AsyncSession, table: str) -> set[str]:
        res = await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=:t"
        ), {"t": table})
        return {r[0] for r in res}

    async def columns(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        res = await session.execute(text("""
            SELECT column_name            AS column_name,
                   data_type              AS data_type,
                   is_nullable            AS is_nullable,
                   column_default         AS column_default,
                   character_maximum_length AS character_maximum_length
            FROM information_schema.columns
            WHERE table_schema=DATABASE() AND table_name=:t
            ORDER BY ordinal_position
        """), {"t": table})
        return [dict(r) for r in res.mappings().all()]

    async def list_tables(self, session: AsyncSession) -> list[dict[str, Any]]:
        res = await session.execute(text(f"""
            SELECT
                t.table_name AS table_name,
                (SELECT COUNT(*) FROM information_schema.columns c
                  WHERE c.table_schema = t.table_schema
                    AND c.table_name = t.table_name)        AS column_count,
                COALESCE(t.table_rows, 0)                   AS row_estimate,
                {_SIZE_EXPR}                                AS total_size,
                NULL AS last_vacuum, NULL AS last_analyze
            FROM information_schema.tables t
            WHERE t.table_schema = DATABASE() AND t.table_type = 'BASE TABLE'
            ORDER BY t.table_name
        """))
        return [dict(r) for r in res.mappings().all()]

    async def schema_summary(self, session: AsyncSession) -> list[dict[str, Any]]:
        # GROUP_CONCAT has a length cap (group_concat_max_len, default 1024); for wide
        # tables the column list is truncated. Acceptable — this only feeds AI prompt
        # context, never a correctness path.
        res = await session.execute(text("""
            SELECT t.table_name AS table_name,
                   GROUP_CONCAT(CONCAT(c.column_name, ' ', c.data_type)
                                ORDER BY c.ordinal_position SEPARATOR ', ') AS cols
            FROM information_schema.tables t
            JOIN information_schema.columns c
                ON c.table_schema = t.table_schema AND c.table_name = t.table_name
            WHERE t.table_schema = DATABASE() AND t.table_type = 'BASE TABLE'
            GROUP BY t.table_name ORDER BY t.table_name
        """))
        return [dict(r) for r in res.mappings().all()]

    async def indexes(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        res = await session.execute(text("""
            SELECT index_name AS index_name,
                   CASE WHEN MIN(non_unique) = 0 THEN 1 ELSE 0 END AS is_unique,
                   CASE WHEN index_name = 'PRIMARY' THEN 1 ELSE 0 END AS is_primary,
                   NULL AS size, NULL AS scans
            FROM information_schema.statistics
            WHERE table_schema = DATABASE() AND table_name = :t
            GROUP BY index_name
            ORDER BY index_name
        """), {"t": table})
        return [dict(r) for r in res.mappings().all()]

    async def foreign_keys(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        res = await session.execute(text("""
            SELECT kcu.column_name             AS column_name,
                   kcu.referenced_table_name   AS foreign_table,
                   kcu.referenced_column_name  AS foreign_column,
                   rc.delete_rule              AS delete_rule
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.referential_constraints rc
                ON kcu.constraint_name = rc.constraint_name
               AND kcu.constraint_schema = rc.constraint_schema
            WHERE kcu.table_schema = DATABASE()
              AND kcu.table_name = :t
              AND kcu.referenced_table_name IS NOT NULL
        """), {"t": table})
        return [dict(r) for r in res.mappings().all()]

    async def table_stats(self, session: AsyncSession, table: str) -> dict[str, Any]:
        res = await session.execute(text("""
            SELECT COALESCE(table_rows, 0) AS n_live_tup, NULL AS n_dead_tup,
                   NULL AS last_vacuum, NULL AS last_analyze, NULL AS last_autoanalyze
            FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = :t
        """), {"t": table})
        return dict(res.mappings().first() or {})

    async def set_statement_timeout(self, session: AsyncSession, ms: int) -> None:
        # MySQL has no transaction-scoped SET LOCAL; max_execution_time is SESSION-scoped
        # and caps SELECT only. Because it persists on the (pooled) connection, the caller
        # MUST reset_statement_timeout() in a finally so it does not leak onto the next
        # user of that connection. DML is NOT capped by this — a known MySQL limitation
        # vs Postgres' SET LOCAL statement_timeout (documented in components/hxdbmanager.md).
        await session.execute(text(f"SET SESSION max_execution_time = {int(ms)}"))

    async def reset_statement_timeout(self, session: AsyncSession) -> None:
        # Restore the connection's default (0 = no per-statement cap / use global) so the
        # 30s ceiling we set does not bleed into the next request on this pooled connection.
        await session.execute(text("SET SESSION max_execution_time = 0"))

    async def explain_json(self, session: AsyncSession, sql: str) -> Any:
        res = await session.execute(text(f"EXPLAIN FORMAT=JSON {sql}"))
        plan = res.scalar_one()
        # MySQL returns the plan as a JSON string; parse it so callers see the same
        # decoded shape Postgres' asyncpg hands back.
        return json.loads(plan) if isinstance(plan, str) else plan

    async def slow_queries(self, session: AsyncSession, limit: int) -> dict[str, Any]:
        # Degraded by design — pg_stat_statements is PostgreSQL-specific.
        return {"available": False, "queries": [],
                "message": "Slow-query stats require pg_stat_statements (PostgreSQL); "
                           "not available on the MySQL backend."}
