"""The commerce order sweep (R5e step 7) — closes order-cases whose order was never written.

commerce-service opens an order's case over the internal API and then writes the
order in its own transaction. If that transaction fails, the case is left with no
order; this closes it (cancelled, audited) after a grace period. While commerce
still runs inside case-service the two commit together, so this finds nothing.
VELARIS_COMMERCE_ORDER_SWEEP=off stops it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 600
SWEEP_STARTUP_DELAY_SECONDS = 120


def enabled() -> bool:
    return os.environ.get("VELARIS_COMMERCE_ORDER_SWEEP", "on").strip().lower() not in (
        "off", "0", "false", "no")


async def cron() -> None:
    from velaris_shared.db.session import get_session_factory

    from case_service.internal_api import order_cases

    if not enabled():
        log.info("Commerce order sweep OFF (VELARIS_COMMERCE_ORDER_SWEEP=off)")
        return
    await asyncio.sleep(SWEEP_STARTUP_DELAY_SECONDS)
    while True:
        try:
            async with get_session_factory()() as session:
                closed = await order_cases.sweep_orphans(session)
                await session.commit()
            if closed:
                log.warning("commerce order sweep: cancelled %d order-case(s) with no order", closed)
        except Exception as exc:                                 # noqa: BLE001
            log.warning("commerce order sweep cycle failed: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
