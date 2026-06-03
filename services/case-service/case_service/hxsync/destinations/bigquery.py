"""HxSync — BigQuery destination adapter (protocol stub).

A real BigQuery connector would use google-cloud-bigquery.
This stub validates config shape and simulates the push so the
pipeline, tests, and Studio UI all work without cloud credentials.
"""
from __future__ import annotations

import time

from case_service.hxsync.protocol import SyncDestinationProtocol, register_destination

_REQUIRED = {"project_id", "dataset_id"}


@register_destination("bigquery")
class BigQueryDestination(SyncDestinationProtocol):

    def health_check(self) -> dict:
        missing = _REQUIRED - set(self.config)
        if missing:
            return {"ok": False, "message": f"Missing config keys: {missing}", "latency_ms": 0}
        return {"ok": True, "message": f'BigQuery {self.config["project_id"]}.{self.config["dataset_id"]} configured (live connection requires google-cloud-bigquery)', "latency_ms": 1}

    def ensure_schema(self, table: str, columns: list[dict]) -> None:
        pass  # would call bigquery.Client().create_table(...)

    def push_rows(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        # Simulates a successful streaming insert
        return len(rows)
