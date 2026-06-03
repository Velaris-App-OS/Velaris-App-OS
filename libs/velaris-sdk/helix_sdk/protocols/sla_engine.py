"""HELIX SDK Protocol: SLA Engine.

Defines the contract for SLA lifecycle management — starting,
pausing, resuming, checking, and handling breaches.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SLAEngine(Protocol):
    """SLA tracking and enforcement.

    The default implementation uses Temporal child workflows
    with timer-based deadline tracking.
    """

    async def start_sla(
        self, case_id: str, sla_policy_id: str, target_id: str
    ) -> dict[str, Any]: ...

    async def pause_sla(
        self,
        case_id: str,
        sla_policy_id: str,
        reason: str | None = None,
    ) -> None: ...

    async def resume_sla(
        self, case_id: str, sla_policy_id: str
    ) -> None: ...

    async def check_sla(
        self, case_id: str, sla_policy_id: str
    ) -> dict[str, Any]: ...

    async def evaluate_all_slas(
        self, case_id: str
    ) -> list[dict[str, Any]]: ...

    async def handle_breach(
        self, case_id: str, sla_policy_id: str
    ) -> None: ...
