"""PostgreSQL implementation of the HxDBManager introspection contract.

The SQL here is moved VERBATIM from the original hxdbmanager.py router (pre-Phase-1b),
so the Postgres behaviour is byte-identical to what shipped — Postgres is the live
production backend and this port must not change a single plan or result column for it.
See ``base.py`` for the return-shape contract.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .base import DbIntrospector


class PostgresIntrospector(DbIntrospector):
    name = "postgresql"

    def quote_ident(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    async def table_exists(self, session: AsyncSession, table: str) -> bool:
        row = await session.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=:t"
        ), {"t": table})
        return row.scalar_one_or_none() is not None

    async def column_names(self, session: AsyncSession, table: str) -> set[str]:
        res = await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"
        ), {"t": table})
        return {r[0] for r in res}

    async def columns(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        res = await session.execute(text("""
            SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t
            ORDER BY ordinal_position
        """), {"t": table})
        return [dict(r) for r in res.mappings().all()]

    async def list_tables(self, session: AsyncSession) -> list[dict[str, Any]]:
        res = await session.execute(text("""
            SELECT
                t.table_name,
                COUNT(c.column_name)                          AS column_count,
                COALESCE(s.n_live_tup, 0)                    AS row_estimate,
                COALESCE(pg_size_pretty(pg_total_relation_size(
                    quote_ident(t.table_name)::regclass)), '?') AS total_size,
                s.last_vacuum, s.last_analyze
            FROM information_schema.tables t
            LEFT JOIN information_schema.columns c
                ON c.table_name = t.table_name AND c.table_schema = 'public'
            LEFT JOIN pg_stat_user_tables s
                ON s.relname = t.table_name
            WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
            GROUP BY t.table_name, s.n_live_tup, s.last_vacuum, s.last_analyze
            ORDER BY t.table_name
        """))
        return [dict(r) for r in res.mappings().all()]

    async def schema_summary(self, session: AsyncSession) -> list[dict[str, Any]]:
        res = await session.execute(text("""
            SELECT t.table_name,
                   string_agg(c.column_name || ' ' || c.data_type, ', '
                              ORDER BY c.ordinal_position) AS cols
            FROM information_schema.tables t
            JOIN information_schema.columns c
                ON c.table_name = t.table_name AND c.table_schema = 'public'
            WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
            GROUP BY t.table_name ORDER BY t.table_name
        """))
        return [dict(r) for r in res.mappings().all()]

    async def indexes(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        res = await session.execute(text("""
            SELECT i.relname AS index_name, ix.indisunique AS is_unique,
                   ix.indisprimary AS is_primary,
                   pg_size_pretty(pg_relation_size(i.oid)) AS size,
                   s.idx_scan AS scans
            FROM pg_class t
            JOIN pg_index ix ON t.oid = ix.indrelid
            JOIN pg_class i  ON i.oid = ix.indexrelid
            LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.oid
            WHERE t.relname = :t AND t.relkind = 'r'
            ORDER BY i.relname
        """), {"t": table})
        return [dict(r) for r in res.mappings().all()]

    async def foreign_keys(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        res = await session.execute(text("""
            SELECT kcu.column_name, ccu.table_name AS foreign_table,
                   ccu.column_name AS foreign_column,
                   rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.referential_constraints rc
                ON tc.constraint_name = rc.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON rc.unique_constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = :t
        """), {"t": table})
        return [dict(r) for r in res.mappings().all()]

    async def table_stats(self, session: AsyncSession, table: str) -> dict[str, Any]:
        res = await session.execute(text("""
            SELECT n_live_tup, n_dead_tup, last_vacuum, last_analyze, last_autoanalyze
            FROM pg_stat_user_tables WHERE relname = :t
        """), {"t": table})
        return dict(res.mappings().first() or {})

    async def set_statement_timeout(self, session: AsyncSession, ms: int) -> None:
        # SET LOCAL scopes the timeout to the current transaction only.
        await session.execute(text(f"SET LOCAL statement_timeout = '{int(ms)}'"))

    async def reset_statement_timeout(self, session: AsyncSession) -> None:
        # No-op: SET LOCAL auto-reverts at transaction end (commit/rollback), so the
        # value never outlives the request. Preserves the original PG behaviour exactly.
        return None

    async def explain_json(self, session: AsyncSession, sql: str) -> Any:
        res = await session.execute(text(f"EXPLAIN (FORMAT JSON) {sql}"))
        return res.scalar_one()

    async def slow_queries(self, session: AsyncSession, limit: int) -> dict[str, Any]:
        try:
            res = await session.execute(text("""
                SELECT query, calls, total_exec_time, mean_exec_time,
                       rows, shared_blks_hit, shared_blks_read
                FROM pg_stat_statements
                ORDER BY total_exec_time DESC
                LIMIT :lim
            """), {"lim": limit})
            return {"available": True, "queries": [dict(r) for r in res.mappings().all()]}
        except Exception:
            return {"available": False, "queries": [],
                    "message": "pg_stat_statements not enabled"}
