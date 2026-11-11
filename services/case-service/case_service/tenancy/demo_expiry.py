"""The demo-tenant expiry job (worker-service, 2026-11-04).

Hourly: a website demo tenant past its expiry is archived (its people are
refused at once — archived tenants are closed since 2026-10-30), and deleted,
accounts first, `VELARIS_DEMO_DELETE_AFTER_DAYS` later. Only tenants carrying
the demo mark (`velaris_shared.tenancy.demo`) are touched; an install with no
demo tenant does nothing. A legal hold keeps the tenant.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 3600
STARTUP_DELAY_SECONDS = 120


async def run_once(session) -> dict[str, int]:
    from case_service.compliance.erasure import erase_tenant_data
    from case_service.legal_hold.service import tenant_active_count
    from velaris_shared.tenancy.demo import expire_due

    async def erase(s, tid):          # a demo tenant WITH cases could not be deleted before (2026-11-10)
        await erase_tenant_data(s, tid, actor="system:demo-expiry")
    return await expire_due(session, tenant_active_count, erase)


async def cron() -> None:
    from velaris_shared.db.session import get_session_factory

    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            async with get_session_factory()() as session:
                out = await run_once(session)
            if any(out.values()):
                log.info("demo-tenant expiry: %s", out)
        except Exception as exc:                                 # noqa: BLE001
            log.warning("demo-tenant expiry cycle failed: %s", exc)
        await asyncio.sleep(INTERVAL_SECONDS)
