"""SD-6: Background task — warn when connector credentials are expiring.

Checks daily; fires an HxStream event and logs a warning when any connector's
credential_expires_at is within WARN_DAYS days.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

logger = logging.getLogger(__name__)

WARN_DAYS    = 7
CHECK_PERIOD = 86_400  # 24 h


async def credential_expiry_monitor() -> None:
    """Long-running background task — runs at startup and every 24 h thereafter."""
    while True:
        try:
            await _check_expiring_credentials()
        except Exception as exc:
            logger.warning("credential_expiry_monitor error: %s", exc)
        await asyncio.sleep(CHECK_PERIOD)


async def _check_expiring_credentials() -> None:
    from case_service.db.session import AsyncSessionLocal
    from case_service.db.models import ConnectorRegistryModel

    cutoff = datetime.now(timezone.utc) + timedelta(days=WARN_DAYS)
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(ConnectorRegistryModel).where(
                    ConnectorRegistryModel.credential_expires_at.isnot(None),
                    ConnectorRegistryModel.credential_expires_at <= cutoff,
                    ConnectorRegistryModel.enabled == True,   # noqa: E712
                )
            )).scalars().all()

            for connector in rows:
                days_left = max(0, (connector.credential_expires_at - datetime.now(timezone.utc)).days)
                logger.warning(
                    "Connector '%s' (id=%s) credentials expire in %d days (%s)",
                    connector.name, connector.id, days_left, connector.credential_expires_at.date(),
                )
                # Emit HxStream warning event
                try:
                    from case_service.hxstream.emitter import emit_trace
                    await emit_trace(
                        event_type="credential_expiry_warning",
                        data={
                            "connector_id":   str(connector.id),
                            "connector_name": connector.name,
                            "connector_type": connector.connector_type,
                            "expires_at":     connector.credential_expires_at.isoformat(),
                            "days_remaining": days_left,
                        },
                        actor="system",
                    )
                except Exception:
                    pass  # HxStream unavailability must never crash the monitor
    except Exception as exc:
        logger.error("credential_expiry_monitor DB error: %s", exc)
