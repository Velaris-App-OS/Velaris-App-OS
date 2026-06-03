"""HELIX SDK Protocol: Rules Engine.

Defines the contract for evaluating business rules against case
context.  The default implementation lives in ``rules-service``.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RulesEngine(Protocol):
    """Business rule evaluation."""

    async def evaluate_rule(
        self, rule_id: str, context: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def evaluate_rules(
        self, rule_ids: list[str], context: dict[str, Any]
    ) -> list[dict[str, Any]]: ...

    async def evaluate_decision_table(
        self, rule_id: str, inputs: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def validate_data(
        self, data_model_id: str, data: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def compute_urgency(
        self, case_id: str, formula: str | None = None
    ) -> float: ...
