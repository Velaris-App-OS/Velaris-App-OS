"""HxGlobal — local/dev region adapter (fully functional stub)."""
from __future__ import annotations

import time
from case_service.hxglobal.protocol import RegionProtocol, register_region


@register_region("local")
class LocalRegion(RegionProtocol):
    """Single-node local region. Used in development and tests."""

    def ping(self) -> dict:
        t0 = time.monotonic()
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "message": "Local region reachable"}

    def active_case_count(self) -> int:
        return 0

    def replication_lag_ms(self) -> int | None:
        return None  # primary — no lag
