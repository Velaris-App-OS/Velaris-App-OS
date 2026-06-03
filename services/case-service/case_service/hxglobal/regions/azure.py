"""HxGlobal — Azure region adapter (protocol stub)."""
from __future__ import annotations

from case_service.hxglobal.protocol import RegionProtocol, register_region


@register_region("azure")
class AzureRegion(RegionProtocol):

    def ping(self) -> dict:
        subscription = self.config.get("subscription_id")
        if not subscription:
            return {"ok": False, "latency_ms": 0, "message": "Missing 'subscription_id' in config"}
        return {"ok": True, "latency_ms": 1, "message": f"Azure subscription={subscription} configured (live connection requires azure-mgmt-core)"}

    def active_case_count(self) -> int:
        return 0

    def replication_lag_ms(self) -> int | None:
        return self.config.get("simulated_lag_ms", 40)
