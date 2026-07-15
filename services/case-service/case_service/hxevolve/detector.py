"""HxEvolve §3.1 Detect — a CONSUMER of the existing process-mining analyzer.

Adds no mining capability. Reads bottlenecks / high-share slow variants /
conformance drift for one case type and emits bounded *optimization candidates*
for the proposer. Purely deterministic — no AI here.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.process_mining import analyzer

MAX_CANDIDATES = 5          # a scan proposes a handful of things, not a firehose
_MIN_OCCURRENCES = 5        # ignore signals backed by fewer events than this


async def detect_candidates(
    session: AsyncSession,
    case_type_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Bounded, ranked optimization candidates for one case type."""
    candidates: list[dict[str, Any]] = []

    # 1. bottlenecks — activities with the longest average duration
    for b in await analyzer.find_bottlenecks(
            session, case_type_id, days, limit=MAX_CANDIDATES,
            tenant_id=tenant_id):
        if b["severity"] == "low" or b["occurrences"] < _MIN_OCCURRENCES:
            continue
        candidates.append({
            "kind": "bottleneck",
            "target": b["activity"],
            "magnitude_seconds": b["avg_duration_seconds"],
            "occurrences": b["occurrences"],
            "severity": b["severity"],
            "window_days": days,
        })

    # 2. conformance drift — actual flow vs the planned definition
    conf = await analyzer.check_conformance(session, case_type_id, days)
    if not conf.get("error"):
        rate = conf.get("conformance_rate")     # analyzer returns a PERCENTAGE
        if rate is not None and rate < 80 and \
                (conf.get("total_cases_analyzed") or 0) >= _MIN_OCCURRENCES:
            candidates.append({
                "kind": "conformance",
                "target": "flow",
                "conformance_rate_pct": rate,
                "skipped_activities": (conf.get("skipped_activities") or [])[:5],
                "unexpected_activities": (conf.get("unexpected_activities") or [])[:5],
                "window_days": days,
            })

    # 3. dominant slow variants — a frequent path that underperforms invites a
    #    routing/reorder proposal
    variants = await analyzer.discover_variants(session, case_type_id, days,
                                                limit=5, tenant_id=tenant_id)
    for v in variants:
        if v["case_count"] >= _MIN_OCCURRENCES and v["percentage"] >= 20:
            candidates.append({
                "kind": "variant",
                "target": " > ".join(v["sequence"][:8]),
                "sequence": v["sequence"],
                "case_count": v["case_count"],
                "share_pct": v["percentage"],
                "window_days": days,
            })
            break               # one dominant-variant candidate is enough

    return candidates[:MAX_CANDIDATES]
