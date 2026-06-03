"""HELIX SDK Protocol: Case Manager.

Defines the contract for case lifecycle management.  The default
implementation lives in ``case-service``; clients can swap in
any conforming backend via the plugin system.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CaseManager(Protocol):
    """Core case lifecycle operations."""

    async def create_case(
        self,
        case_type_id: str,
        data: dict[str, Any] | None = None,
        priority: str | None = None,
        parent_case_id: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_case(self, case_id: str) -> dict[str, Any]: ...

    async def update_case_data(
        self,
        case_id: str,
        data: dict[str, Any],
        updated_by: str | None = None,
    ) -> dict[str, Any]: ...

    async def transition_stage(
        self,
        case_id: str,
        target_stage_id: str,
        actor_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def change_status(
        self,
        case_id: str,
        status: str,
        reason: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def change_priority(
        self,
        case_id: str,
        priority: str,
        actor_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def resolve_case(
        self,
        case_id: str,
        resolution: dict[str, Any] | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def close_case(
        self, case_id: str, actor_id: str | None = None
    ) -> dict[str, Any]: ...

    async def reopen_case(
        self,
        case_id: str,
        reason: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def cancel_case(
        self,
        case_id: str,
        reason: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def add_relationship(
        self,
        case_id: str,
        target_case_id: str,
        relationship_type: str,
    ) -> dict[str, Any]: ...

    async def create_child_case(
        self,
        parent_case_id: str,
        case_type_id: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def search_cases(
        self,
        filters: dict[str, Any],
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]: ...

    async def get_case_history(
        self, case_id: str
    ) -> list[dict[str, Any]]: ...
