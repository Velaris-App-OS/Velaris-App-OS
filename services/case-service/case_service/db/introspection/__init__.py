"""Per-dialect database introspection for HxDBManager (DB SDK Phase 1b).

The router (`api/routers/hxdbmanager.py`) calls ``get_introspector(session)`` and then
the dialect-neutral methods on the returned object — it never writes dialect-specific
SQL itself. Each dialect lives in its own file:

    postgres.py  -> PostgresIntrospector   (verbatim from the original PG router)
    mysql.py     -> MysqlIntrospector       (information_schema / DATABASE() equivalents)

Dispatch is on the session's REAL bind dialect (``session.get_bind().dialect.name``),
not config — same reasoning as hxguard/tuples.py: the test harness can run a SQLite
engine while config says postgresql, so the live engine is the only correct source.

Adding a dialect = add a file + one ``_INTROSPECTORS`` entry. An unsupported dialect
fails loud (ValueError) rather than silently returning wrong/empty results.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from .base import DbIntrospector
from .mysql import MysqlIntrospector
from .postgres import PostgresIntrospector

# Allowlist of supported dialects → introspector singletons (the classes are stateless).
_INTROSPECTORS: dict[str, DbIntrospector] = {
    "postgresql": PostgresIntrospector(),
    "mysql": MysqlIntrospector(),
}


def get_introspector(session: AsyncSession) -> DbIntrospector:
    """Return the introspector for ``session``'s live bind dialect, or fail loud."""
    dialect = session.get_bind().dialect.name
    insp = _INTROSPECTORS.get(dialect)
    if insp is None:
        raise ValueError(
            f"HxDBManager has no introspector for dialect {dialect!r}. "
            f"Supported: {sorted(_INTROSPECTORS)}."
        )
    return insp


__all__ = [
    "DbIntrospector",
    "MysqlIntrospector",
    "PostgresIntrospector",
    "get_introspector",
]
