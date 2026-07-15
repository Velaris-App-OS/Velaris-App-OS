"""HxDBMigrate P2 — source→Velaris(Postgres) type-mapping hints + conversion warnings.

Deterministic mapping from a source column's ``data_type`` to the recommended Postgres
target type, flagging conversions that need a human decision (e.g. MySQL TINYINT(1) that is
really a boolean, ENUM/SET with no direct PG equivalent, timezone handling).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

# base_type → (postgres_target, warning|None)
_MAP: dict[str, tuple[str, str | None]] = {
    "tinyint":    ("smallint", "MySQL TINYINT(1) is often a boolean — verify intent"),
    "smallint":   ("smallint", None),
    "mediumint":  ("integer", None),
    "int":        ("integer", None),
    "integer":    ("integer", None),
    "bigint":     ("bigint", None),
    "decimal":    ("numeric", None),
    "numeric":    ("numeric", None),
    "double":     ("double precision", None),
    "float":      ("real", None),
    "bit":        ("boolean", None),
    "bool":       ("boolean", None),
    "boolean":    ("boolean", None),
    "char":       ("char", None),
    "varchar":    ("varchar", None),
    "tinytext":   ("text", None),
    "text":       ("text", None),
    "mediumtext": ("text", None),
    "longtext":   ("text", None),
    "json":       ("jsonb", None),
    "uuid":       ("uuid", None),
    "date":       ("date", None),
    "time":       ("time", None),
    "datetime":   ("timestamp", None),
    "timestamp":  ("timestamptz", "verify source timezone handling before mapping to timestamptz"),
    "year":       ("integer", None),
    "enum":       ("varchar", "MySQL ENUM → varchar + CHECK constraint or a lookup table"),
    "set":        ("varchar", "MySQL SET has no PG equivalent — model as an array or lookup table"),
    "blob":       ("bytea", None),
    "tinyblob":   ("bytea", None),
    "mediumblob": ("bytea", None),
    "longblob":   ("bytea", "large binary — consider object storage instead of a column"),
    "binary":     ("bytea", None),
    "varbinary":  ("bytea", None),
    # postgres source (upgrade/consolidation) — mostly identity
    "character varying": ("varchar", None),
    "timestamp with time zone": ("timestamptz", None),
    "timestamp without time zone": ("timestamp", None),
    "jsonb":      ("jsonb", None),
}


def map_type(data_type: str) -> dict[str, Any]:
    """Return {target_type, warning} for a source column data_type."""
    base = (data_type or "").strip().lower().split("(")[0].strip()
    target, warning = _MAP.get(base, ("text", "unrecognised type — defaulting to text; verify manually"))
    return {"target_type": target, "warning": warning}
