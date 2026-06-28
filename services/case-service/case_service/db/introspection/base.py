"""DB-introspection contract for HxDBManager (DB SDK Phase 1b).

HxDBManager (P67) is an admin tool that browses the platform's OWN database — table
list, columns, indexes, foreign keys, sizes, EXPLAIN plans, slow queries. All of that
is dialect-specific: Postgres answers via `pg_*` system catalogs + `information_schema`;
MySQL answers via `information_schema` + `performance_schema` with different column
names, no `pg_size_pretty`, no `SET LOCAL`, etc.

Rather than scatter `if dialect == ...` branches through the 1600-line router, each
dialect implements this one interface in its own file (`postgres.py`, `mysql.py`) and
the router calls `get_introspector(session).<method>(...)`. So:

  * one method name, one return shape, two implementations;
  * every dialect's quirks live in exactly one findable place;
  * adding a dialect = adding one file + one allowlist entry (see __init__.py).

RETURN-SHAPE CONTRACT (load-bearing — the router and the Studio UI depend on these
exact keys, identical across dialects; a backend with no value for a key returns None,
never omits it):

  list_tables(...)   -> [{table_name, column_count, row_estimate, total_size,
                          last_vacuum, last_analyze}]
  columns(...)       -> [{column_name, data_type, is_nullable, column_default,
                          character_maximum_length}]   (ordinal order)
  indexes(...)       -> [{index_name, is_unique, is_primary, size, scans}]
  foreign_keys(...)  -> [{column_name, foreign_table, foreign_column, delete_rule}]
  table_stats(...)   -> {n_live_tup, n_dead_tup, last_vacuum, last_analyze,
                          last_autoanalyze}
  schema_summary(...)-> [{table_name, cols}]   (cols = "name type, name type, …")
  slow_queries(...)  -> {available: bool, queries: [...], message?: str}

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import abc
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class DbIntrospector(abc.ABC):
    """Per-dialect database-introspection primitives for HxDBManager."""

    #: dialect name this introspector serves (matches SQLAlchemy ``dialect.name``)
    name: str = ""

    # ── identifier quoting ────────────────────────────────────────────────────
    @abc.abstractmethod
    def quote_ident(self, identifier: str) -> str:
        """Quote a (pre-validated) table/column name for this dialect.

        Callers MUST validate the identifier against the live schema first
        (``table_exists`` / ``column_names``); this only applies the correct
        quote char (``"x"`` on Postgres, `` `x` `` on MySQL)."""

    # ── existence / columns ───────────────────────────────────────────────────
    @abc.abstractmethod
    async def table_exists(self, session: AsyncSession, table: str) -> bool:
        """True if a BASE TABLE named ``table`` exists in the active schema."""

    @abc.abstractmethod
    async def column_names(self, session: AsyncSession, table: str) -> set[str]:
        """Set of column names for ``table`` (for sort/identifier validation)."""

    @abc.abstractmethod
    async def columns(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        """Column metadata in ordinal order. See module RETURN-SHAPE CONTRACT."""

    # ── schema overview ───────────────────────────────────────────────────────
    @abc.abstractmethod
    async def list_tables(self, session: AsyncSession) -> list[dict[str, Any]]:
        """All base tables with column count, row estimate and size."""

    @abc.abstractmethod
    async def schema_summary(self, session: AsyncSession) -> list[dict[str, Any]]:
        """Compact per-table column list for AI prompt context."""

    # ── table detail ──────────────────────────────────────────────────────────
    @abc.abstractmethod
    async def indexes(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        """Index metadata for ``table``."""

    @abc.abstractmethod
    async def foreign_keys(self, session: AsyncSession, table: str) -> list[dict[str, Any]]:
        """Outbound foreign keys for ``table``."""

    @abc.abstractmethod
    async def table_stats(self, session: AsyncSession, table: str) -> dict[str, Any]:
        """Row/vacuum statistics for ``table`` (None for stats this dialect lacks)."""

    # ── query execution helpers ───────────────────────────────────────────────
    @abc.abstractmethod
    async def set_statement_timeout(self, session: AsyncSession, ms: int) -> None:
        """Apply a per-statement timeout to the current session/transaction.

        NOTE the dialect difference callers must account for: Postgres' ``SET LOCAL``
        is transaction-scoped and caps ALL statements; MySQL's ``max_execution_time``
        is session-scoped (so it must be reset — see ``reset_statement_timeout``) and
        caps SELECT only (DML runs uncapped on MySQL)."""

    @abc.abstractmethod
    async def reset_statement_timeout(self, session: AsyncSession) -> None:
        """Undo ``set_statement_timeout``. MUST be called in a finally after the user
        query so a session-scoped timeout (MySQL) does not leak onto the next caller of
        a pooled connection. No-op where the timeout is transaction-scoped (Postgres)."""

    @abc.abstractmethod
    async def explain_json(self, session: AsyncSession, sql: str) -> Any:
        """Return the JSON query plan for ``sql`` (planning only, never executes)."""

    @abc.abstractmethod
    async def slow_queries(self, session: AsyncSession, limit: int) -> dict[str, Any]:
        """Slowest queries, or ``{"available": False, ...}`` where unsupported."""
