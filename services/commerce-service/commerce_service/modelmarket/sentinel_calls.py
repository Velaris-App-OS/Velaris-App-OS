"""The model market's calls into HxSentinel — answered by case-service.

HxSentinel's history queries and its pack table are case-service's (R5e step 7:
it cannot move — it reaches retention, HxNexus and legal hold), so the model
market asks over the internal API. Each wrapper returns exactly what the direct
call used to, so the scoring code around it is unchanged.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.internal import client as internal


async def labelled_features(session: AsyncSession, tenant: str) -> tuple[np.ndarray, np.ndarray]:
    """`sentinel.model._labelled_features` — the tenant's labelled history."""
    answer = await internal.call("sentinel/labelled-features", {"tenant": tenant}, session=session)
    return np.array(answer["X"], dtype=float), np.array(answer["y"], dtype=float)


async def feature_contexts(session: AsyncSession, tenant: str,
                           case_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    """`sentinel.packs.build_feature_context` for each case; failures skipped, as before."""
    answer = await internal.call("sentinel/feature-contexts",
                                 {"tenant": tenant, "case_ids": [str(c) for c in case_ids]},
                                 session=session)
    return answer["contexts"]


async def install_signal_pack(session: AsyncSession, *, tenant: str, manifest: dict,
                              installed_by: str, enabled: bool = False) -> str:
    """`sentinel.packs.install_pack`, left disabled unless asked; returns the row id."""
    answer = await internal.call("sentinel/signal-packs", {
        "tenant": tenant, "manifest": manifest, "installed_by": installed_by,
        "enabled": enabled}, session=session)
    return answer["id"]
