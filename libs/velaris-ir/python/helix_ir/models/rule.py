"""HELIX IR: Rule definitions.

Rules are the decision logic layer.  They evaluate conditions, compute
values, route work, and enforce policies.  Executed by the rules-service
(``RulesEngine`` protocol).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class RuleType(enum.Enum):
    """Kinds of business rules the engine can evaluate."""

    WHEN = "when"
    DECISION_TABLE = "decision_table"
    DECISION_TREE = "decision_tree"
    MAP_VALUE = "map_value"
    EXPRESSION = "expression"
    VALIDATION = "validation"
    ROUTING = "routing"
    SLA_CALCULATION = "sla_calculation"
    URGENCY = "urgency"
    DECLARE_EXPRESSION = "declare_expression"
    CONSTRAINT = "constraint"


class RuleScope(enum.Enum):
    """Where a rule is applicable."""

    GLOBAL = "global"
    CASE_TYPE = "case_type"
    STAGE = "stage"
    STEP = "step"


@dataclass(frozen=True)
class RuleCondition:
    """A single predicate in a rule."""

    field_path: str
    operator: str  # eq, neq, gt, gte, lt, lte, in, not_in, contains, etc.
    value: Any = None
    value_field_path: str | None = None


@dataclass(frozen=True)
class RuleAction:
    """Side effect executed when rule conditions are met."""

    action_type: str  # set_value, invoke_rule, send_notification, assign_to, …
    target: str | None = None
    value: Any = None
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionTableColumn:
    """Column definition for a decision table rule."""

    id: str
    name: str
    field_path: str
    is_condition: bool = True
    allowed_values: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionTableRow:
    """One row in a decision table."""

    conditions: dict[str, Any] = field(default_factory=dict)
    outcomes: dict[str, Any] = field(default_factory=dict)
    priority: int = 0


@dataclass
class RuleDefinition:
    """A business rule evaluated against case context.

    The ``RulesEngine`` protocol implementation evaluates these to
    produce decisions, validations, computed values, or side effects.
    """

    id: str
    name: str
    version: str
    rule_type: RuleType
    scope: RuleScope = RuleScope.GLOBAL
    scope_target_id: str | None = None
    description: str = ""
    # WHEN rules
    conditions: list[RuleCondition] = field(default_factory=list)
    actions: list[RuleAction] = field(default_factory=list)
    # Decision tables
    table_columns: list[DecisionTableColumn] = field(default_factory=list)
    table_rows: list[DecisionTableRow] = field(default_factory=list)
    # Expression / declare-expression
    expression: str | None = None
    result_field_path: str | None = None
    # Metadata
    priority: int = 0
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
