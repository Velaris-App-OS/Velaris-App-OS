"""HxGlobal — GCP region adapter (protocol stub)."""
from __future__ import annotations

from case_service.hxglobal.protocol import RegionProtocol, register_region


@register_region("gcp")
class GCPRegion(RegionProtocol):

    def ping(self) -> dict:
        project = self.config.get("project_id")
        if not project:
            return {"ok": False, "latency_ms": 0, "message": "Missing 'project_id' in config"}
        return {"ok": True, "latency_ms": 1, "message": f"GCP project={project} configured (live connection requires google-cloud-core)"}

    def active_case_count(self) -> int:
        return 0

    def replication_lag_ms(self) -> int | None:
        return self.config.get("simulated_lag_ms", 30)
