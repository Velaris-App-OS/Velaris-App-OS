"""Marketplace capability-grant ENFORCEMENT — the checks case-service runs.

Moved out of `case_service.marketplace.grants` (2026-10-19, step 7 — the commerce
split): grant MANAGEMENT moves to commerce-service with the marketplace, but the
broker and the HxBridge connector keep enforcing grants in case-service, so the
checks they call live in the shared layer. Unchanged; `grants` imports them back.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger("case_service.marketplace.grants")   # the log name it always had

CONTAINER_DESCRIPTOR_FORMAT = "container_image"


# ── runtime enforcement helpers (called from http_custom_connector) ──

def host_allowed(host: str, granted_domains: list[str]) -> bool:
    """Exact host or subdomain of a granted domain — never substring tricks."""
    host = (host or "").lower().rstrip(".")
    for d in granted_domains:
        d = str(d).lower().rstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


async def log_marketplace_call(
    *,
    grant_id: str | None,
    package_id: str,
    url: str,
    method: str,
    status: str,               # allowed | blocked
    http_status_code: int | None = None,
    is_declared: bool = True,
) -> None:
    """Audit every capability use — own session, never blocks the call path.
    One retry: a transient lock (shared dev SQLite) must not lose audit rows."""
    import asyncio

    from velaris_shared.db.models import MarketplaceNetworkLogModel
    from velaris_shared.db.session import get_session_factory
    for attempt in (1, 2):
        try:
            factory = get_session_factory()
            async with factory() as session:
                session.add(MarketplaceNetworkLogModel(
                    workspace_id=None,
                    grant_id=uuid.UUID(grant_id) if grant_id else None,
                    package_id=package_id,
                    destination_url=url[:1024],
                    http_method=method[:16],
                    status=status,
                    http_status_code=http_status_code,
                    is_declared=is_declared,
                ))
                await session.commit()
            return
        except Exception as exc:
            if attempt == 2:
                logger.warning("marketplace network log write failed: %s", exc)
            else:
                await asyncio.sleep(0.05)
