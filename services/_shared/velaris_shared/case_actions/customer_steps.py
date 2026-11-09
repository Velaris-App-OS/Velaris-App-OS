"""Which steps of a stage a portal customer may act on (R5e step 8).

One rule, read by portal-service (to show and accept an action) and by
case-service's internal API (to re-check before it writes), so the two can
never disagree about what a customer may complete.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any


def customer_steps(ct: Any, stage_id: str | None) -> list[dict]:
    """Customer-assigned steps of one stage from the case-type definition."""
    if not ct or not stage_id:
        return []
    stage = next((s for s in (ct.definition_json or {}).get("stages", [])
                  if s.get("id") == stage_id), None)
    if not stage:
        return []
    return [
        s for s in stage.get("steps", [])
        if (s.get("assignment") or {}).get("strategy") == "customer"
    ]
