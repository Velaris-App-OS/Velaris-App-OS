#!/usr/bin/env python
"""Regenerate the MySQL consolidated baseline from the ORM metadata (DB SDK Phase 1).

Emits exactly the DDL `Base.metadata.create_all` would run on MySQL — wrapped with
FK-checks-off so the create order (sorted_tables) never trips a forward FK reference.

Usage:
    uv run python scripts/gen_mysql_baseline.py            # writes migrations/mysql/0001_baseline.sql
    uv run python scripts/gen_mysql_baseline.py --check     # non-zero exit if the file is stale

After running, hand-review the diff. The runner (start-velaris.sh) applies this single
file as the MySQL track, recorded in schema_migrations by basename.
"""
from __future__ import annotations

import sys
from pathlib import Path

# case-service must be importable (its db.models holds the metadata).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "case-service"))

from sqlalchemy.dialects import mysql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from case_service.db.models import Base  # noqa: E402

OUT = ROOT / "migrations" / "mysql" / "0001_baseline.sql"

HEADER = """\
-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ consolidated baseline (DB SDK Phase 1).
--
-- Generated from the SQLAlchemy ORM metadata (case_service.db.models) compiled
-- to the MySQL dialect — the same DDL `Base.metadata.create_all` emits. This is
-- the MySQL equivalent of the Postgres `migrations/postgresql/*.sql` track: ONE baseline
-- instead of the 80+ incremental PG files (Velaris ships fresh on MySQL; there
-- is no in-place PG→MySQL upgrade path — that is HxDBMigrate's job).
--
-- Recommended: create the database as utf8mb4
--   (CREATE DATABASE velaris CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;)
-- The indexed-identifier columns are bounded for InnoDB's 3072-byte key limit at
-- utf8mb4's 4 bytes/char, so this schema is safe on any charset.
--
-- DO NOT EDIT BY HAND. Regenerate via scripts/gen_mysql_baseline.py after a model
-- change, and hand-review the diff.
-- ============================================================================

SET FOREIGN_KEY_CHECKS = 0;
"""
FOOTER = "\nSET FOREIGN_KEY_CHECKS = 1;\n"


def render() -> str:
    # Deterministic emission: `create_all` orders CREATE INDEX off a set (varies per
    # process), so build the DDL ourselves — tables in dependency order (sorted_tables,
    # so inline FKs always reference an already-created table), then each table's
    # indexes sorted by name. Content matches create_all; order is stable across runs.
    dialect = mysql.dialect()
    stmts: list[str] = []
    for table in Base.metadata.sorted_tables:
        stmts.append(str(CreateTable(table).compile(dialect=dialect)).strip())
        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            stmts.append(str(CreateIndex(idx).compile(dialect=dialect)).strip())
    body = ";\n\n".join(stmts) + ";\n"
    return HEADER + "\n" + body + FOOTER


def main() -> int:
    sql = render()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != sql:
            print(f"STALE: {OUT} is out of date — run: uv run python scripts/gen_mysql_baseline.py")
            return 1
        print(f"OK: {OUT} is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(sql)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
