"""HxSync — DuckDB destination adapter (fully functional, in-process)."""
from __future__ import annotations

import time
from typing import Any

from case_service.hxsync.protocol import SyncDestinationProtocol, register_destination


@register_destination("duckdb")
class DuckDBDestination(SyncDestinationProtocol):
    """Writes to a local DuckDB file.  Used for dev, testing, and self-hosted DWH."""

    def health_check(self) -> dict:
        t0 = time.monotonic()
        try:
            import duckdb  # type: ignore
            path = self.config.get("path", ":memory:")
            con = duckdb.connect(path)
            con.execute("SELECT 1").fetchone()
            con.close()
            return {"ok": True, "message": "DuckDB reachable", "latency_ms": int((time.monotonic() - t0) * 1000)}
        except ImportError:
            return {"ok": False, "message": "duckdb package not installed — pip install duckdb", "latency_ms": 0}
        except Exception as exc:
            return {"ok": False, "message": str(exc), "latency_ms": int((time.monotonic() - t0) * 1000)}

    def ensure_schema(self, table: str, columns: list[dict]) -> None:
        try:
            import duckdb
        except ImportError:
            return
        path = self.config.get("path", ":memory:")
        col_defs = ", ".join(f'{c["name"]} {c.get("type", "VARCHAR")}' for c in columns)
        with duckdb.connect(path) as con:
            con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({col_defs})")
            existing = {row[0] for row in con.execute(f"DESCRIBE {table}").fetchall()}
            for col in columns:
                if col["name"] not in existing:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {col["name"]} {col.get("type", "VARCHAR")}')

    def push_rows(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        try:
            import duckdb
        except ImportError:
            return 0
        path = self.config.get("path", ":memory:")
        cols = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        values = [[r.get(c) for c in cols] for r in rows]
        with duckdb.connect(path) as con:
            con.executemany(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})", values)
        return len(rows)
