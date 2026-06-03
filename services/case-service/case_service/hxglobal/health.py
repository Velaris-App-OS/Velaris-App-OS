"""HxGlobal — region health checker.

poll_region_health(region, session) snapshots latency + case count,
writes a RegionHealthLogModel row, and fires HxStream events on
status transitions (degraded ↔ recovered).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import RegionModel, RegionHealthLogModel
from case_service.hxglobal import regions as _  # noqa: F401
from case_service.hxglobal.protocol import get_region_adapter

_LATENCY_DEGRADED_MS = 500


async def poll_region_health(region: RegionModel, session: AsyncSession) -> dict:
    """Ping region, write health log, return summary."""
    try:
        adapter = get_region_adapter(region.provider, region.connection_config)
        ping = adapter.ping()
        lag = adapter.replication_lag_ms()
        status = "healthy" if ping["ok"] and ping["latency_ms"] < _LATENCY_DEGRADED_MS else "degraded"
        error_msg = None if ping["ok"] else ping.get("message")
    except Exception as exc:
        ping = {"ok": False, "latency_ms": 0}
        lag = None
        status = "unreachable"
        error_msg = str(exc)

    log = RegionHealthLogModel(
        region_id=region.id,
        status=status,
        latency_ms=ping["latency_ms"],
        replication_lag_ms=lag,
        error_msg=error_msg,
        recorded_at=datetime.now(timezone.utc),
    )
    session.add(log)

    try:
        from case_service.hxstream.emitter import emit_event
        if status in ("degraded", "unreachable"):
            await emit_event(session, "region_degraded", {
                "region_id": str(region.id), "region_name": region.name,
                "status": status, "latency_ms": ping["latency_ms"],
            })
        elif status == "healthy":
            await emit_event(session, "region_recovered", {
                "region_id": str(region.id), "region_name": region.name,
            })
    except Exception:
        pass

    return {
        "region_id": str(region.id), "region_name": region.name,
        "status": status, "latency_ms": ping["latency_ms"],
        "replication_lag_ms": lag, "error_msg": error_msg,
    }
