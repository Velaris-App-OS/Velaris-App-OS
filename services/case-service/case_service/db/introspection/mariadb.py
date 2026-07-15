"""MariaDB implementation of the HxDBManager introspection contract.

MariaDB is information_schema-compatible with MySQL, so this reuses `MysqlIntrospector`
wholesale and overrides only the genuine divergence:

  * statement timeout — MySQL uses ``max_execution_time`` (**milliseconds**, caps
    ``SELECT`` only); MariaDB has no such variable and instead uses
    ``max_statement_time`` (**seconds**, decimal, caps **all** statements incl. DML —
    stricter, and closer to Postgres' ``SET LOCAL statement_timeout``). Sending MySQL's
    variable to MariaDB errors with 1193 "Unknown system variable", so it must be
    overridden here.

Dispatch to this class happens in ``introspection/__init__.py`` on the live bind's
``dialect._is_mariadb`` flag (SQLAlchemy reports ``dialect.name == "mysql"`` for a
MariaDB server reached via the mysql driver), so a MariaDB server is routed here even
when the operator configured ``database: mysql``.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .mysql import MysqlIntrospector


class MariadbIntrospector(MysqlIntrospector):
    name = "mariadb"

    async def set_statement_timeout(self, session: AsyncSession, ms: int) -> None:
        # MariaDB: max_statement_time is in SECONDS (decimal ok) and caps ALL statements
        # (incl. DML), unlike MySQL's max_execution_time (ms, SELECT-only). It is
        # SESSION-scoped and persists on the pooled connection, so callers MUST
        # reset_statement_timeout() in a finally.
        seconds = max(int(ms), 0) / 1000.0
        await session.execute(text(f"SET SESSION max_statement_time = {seconds}"))

    async def reset_statement_timeout(self, session: AsyncSession) -> None:
        # Restore the connection default (0 = no per-statement cap) so the ceiling does
        # not leak onto the next request on this pooled connection.
        await session.execute(text("SET SESSION max_statement_time = 0"))

    async def slow_queries(self, session: AsyncSession, limit: int) -> dict[str, Any]:
        # Degraded by design — pg_stat_statements is PostgreSQL-specific.
        return {"available": False, "queries": [],
                "message": "Slow-query stats require pg_stat_statements (PostgreSQL); "
                           "not available on the MariaDB backend."}
