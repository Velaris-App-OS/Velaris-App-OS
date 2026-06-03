"""HELIX SDK Protocol: Work Queue Manager.

Defines the contract for work queue operations — fetching items,
claiming, releasing, assigning, and completing work.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WorkQueueManager(Protocol):
    """Work queue query and mutation operations.

    Queues are live views over assignments — the implementation
    queries ``case_assignments`` and applies queue filter/sort
    definitions at query time.
    """

    async def get_queue_items(
        self,
        queue_id: str,
        page: int = 1,
        page_size: int = 50,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def claim_item(
        self, queue_id: str, item_id: str, user_id: str
    ) -> dict[str, Any]: ...

    async def release_item(
        self, item_id: str, user_id: str
    ) -> dict[str, Any]: ...

    async def assign_item(
        self,
        item_id: str,
        assignee_id: str,
        assigned_by: str | None = None,
    ) -> dict[str, Any]: ...

    async def reassign_item(
        self,
        item_id: str,
        new_assignee_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]: ...

    async def complete_item(
        self,
        item_id: str,
        result: dict[str, Any] | None = None,
        completed_by: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_user_workload(
        self, user_id: str
    ) -> dict[str, Any]: ...

    async def get_queue_stats(
        self, queue_id: str
    ) -> dict[str, Any]: ...
