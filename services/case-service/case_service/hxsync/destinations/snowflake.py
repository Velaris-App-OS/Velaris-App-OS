"""HxSync — Snowflake destination adapter (protocol stub).

Real connector would use snowflake-connector-python.
"""
from __future__ import annotations

from case_service.hxsync.protocol import SyncDestinationProtocol, register_destination

_REQUIRED = {"account", "database", "schema", "warehouse"}


@register_destination("snowflake")
class SnowflakeDestination(SyncDestinationProtocol):

    def health_check(self) -> dict:
        missing = _REQUIRED - set(self.config)
        if missing:
            return {"ok": False, "message": f"Missing config keys: {missing}", "latency_ms": 0}
        return {"ok": True, "message": f'Snowflake {self.config["account"]}.{self.config["database"]} configured (live connection requires snowflake-connector-python)', "latency_ms": 1}

    def ensure_schema(self, table: str, columns: list[dict]) -> None:
        pass

    def push_rows(self, table: str, rows: list[dict]) -> int:
        return len(rows)
