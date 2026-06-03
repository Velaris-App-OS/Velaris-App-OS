"""HxGlobal — AWS region adapter (protocol stub)."""
from __future__ import annotations

from case_service.hxglobal.protocol import RegionProtocol, register_region

_REQUIRED = {"region", "endpoint"}


@register_region("aws")
class AWSRegion(RegionProtocol):

    def ping(self) -> dict:
        missing = _REQUIRED - set(self.config)
        if missing:
            return {"ok": False, "latency_ms": 0, "message": f"Missing config keys: {missing}"}
        return {"ok": True, "latency_ms": 1, "message": f'AWS {self.config["region"]} configured (live connection requires boto3)'}

    def active_case_count(self) -> int:
        return 0

    def replication_lag_ms(self) -> int | None:
        return self.config.get("simulated_lag_ms", 50)
