"""Released-feature flags — the per-process cache every service reads.

Moved out of `case_service.api.routers.releases` (2026-10-19, step 7 — the
commerce split): the marketplace and the model market (commerce-service) gate
routes on these flags, so the cache, its reload and its change listener live in
the shared layer. One dict per process; the releases router re-exports this very
object, so every existing reader and test that mutates it is unchanged.

A process fills it with `refresh_version_cache` at start-up and whenever another
process bumps the `flags` generation. Until the first fill every flag reads as
off — the behaviour the releases router has always had in its first seconds.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import generations as _generations

log = logging.getLogger(__name__)

_ENABLED_VERSIONS: dict[str, str | None] = {}  # feature_key -> version string or None


def is_feature_enabled(feature_key: str) -> bool:
    """Check if a feature is enabled.

    Returns True if the DB has any non-null version for this feature key.
    Version comparison is the DB's concern — the code only needs on/off.
    Reads from the in-memory cache populated by release_cron() — no DB call.
    Returns False during the first ~10 seconds after startup (before first cron cycle).
    """
    return _ENABLED_VERSIONS.get(feature_key) is not None


async def refresh_version_cache(session: AsyncSession) -> None:
    """Reload _ENABLED_VERSIONS from DB. Called by the cron after each cycle."""
    from velaris_shared.db.models import ScheduledReleaseModel
    rows = (await session.execute(select(ScheduledReleaseModel))).scalars().all()
    _ENABLED_VERSIONS.clear()
    for r in rows:
        _ENABLED_VERSIONS[r.feature_key] = r.enabled
    log.info("Feature flag cache refreshed: %s", dict(_ENABLED_VERSIONS))


# A flag changed in another process: reload now, not at this process's next
# hourly cron (R5e step 5 — velaris_shared/generations.py).
_generations.on_change("flags", refresh_version_cache)
