"""HxSandbox #17 Phase 1 — safe expression evaluator tests.

Covers the security contract (sandbox-escape attempts are rejected, never
evaluated), grammar coverage (CONFORMING expressions evaluate identically to
the old restricted eval), classification (CONFORMING / NEEDS_MIGRATION /
REJECTED), and the resource caps.

Pure-function tests: no DB, no Temporal, no FastAPI.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from case_service.core.safe_expression import (
    Classification,
    MAX_DEPTH,
    MAX_NODES,
    classify_expression,
    evaluate,
)


# ─── Sandbox-escape battery — all REJECTED, all evaluate to None ──

ESCAPES = [
    "().__class__.__bases__[0].__subclasses__()",
    "(1).__class__",
    "''.__class__.__mro__",
    "__import__('os').system('id')",
    "__builtins__",
    "().__class__.__base__.__subclasses__()[0]",
    "type(1).__mro__",          # type() is not whitelisted, attr is rejected
    "(lambda: 1)()",
    "[x for x in ().__class__.__subclasses__()]",
]


@pytest.mark.parametrize("expr", ESCAPES)
def test_escape_attempts_rejected(expr):
    cls, reason = classify_expression(expr)
    assert cls is Classification.REJECTED, f"{expr} -> {cls} ({reason})"
    assert evaluate(expr, {}) is None


def test_attribute_access_always_rejected():
    cls, _ = classify_expression("obj.attr")
    assert cls is Classification.REJECTED


def test_dunder_name_rejected():
    cls, _ = classify_expression("__class__")
    assert cls is Classification.REJECTED


# ─── Grammar coverage — CONFORMING, evaluated by the pure walker ──

CONFORMING_CASES = [
    ("amount * 0.1", {"amount": 500}, 50.0),
    ("a + b - c", {"a": 10, "b": 5, "c": 3}, 12),
    ("a / b", {"a": 9, "b": 2}, 4.5),
    ("a // b", {"a": 9, "b": 2}, 4),
    ("a % b", {"a": 9, "b": 2}, 1),
    ("a ** b", {"a": 2, "b": 3}, 8),
    ("-a", {"a": 5}, -5),
    ("not flag", {"flag": False}, True),
    ("a > b", {"a": 5, "b": 3}, True),
    ("lo <= x <= hi", {"lo": 0, "x": 5, "hi": 10}, True),
    ("a and b", {"a": True, "b": False}, False),
    ("a or b", {"a": False, "b": 7}, 7),
    ("x if cond else y", {"x": 1, "y": 2, "cond": True}, 1),
    ("status in allowed", {"status": "open", "allowed": ["open", "closed"]}, True),
    ("status not in blocked", {"status": "ok", "blocked": ["bad"]}, True),
    ("min(a, b)", {"a": 5, "b": 3}, 3),
    ("max(a, b)", {"a": 5, "b": 3}, 5),
    ("round(x, n)", {"x": 3.14159, "n": 2}, 3.14),
    ("abs(x)", {"x": -7}, 7),
    ("len(items)", {"items": [1, 2, 3]}, 3),
    ("sum(items)", {"items": [1, 2, 3]}, 6),
    ("int(x)", {"x": "42"}, 42),
    ("str(x)", {"x": 42}, "42"),
    ("bool(x)", {"x": 0}, False),
    ("any(items)", {"items": [0, 0, 1]}, True),
    ("all(items)", {"items": [1, 1, 1]}, True),
]


@pytest.mark.parametrize("expr,names,expected", CONFORMING_CASES)
def test_conforming_evaluates_correctly(expr, names, expected):
    cls, _ = classify_expression(expr)
    assert cls is Classification.CONFORMING, expr
    assert evaluate(expr, names) == expected


def test_short_circuit_and_returns_falsy_operand():
    assert evaluate("a and b", {"a": "", "b": "x"}) == ""


def test_short_circuit_or_returns_truthy_operand():
    assert evaluate("a or b", {"a": 0, "b": "fallback"}) == "fallback"


# ─── NEEDS_MIGRATION — eval-safe but outside the strict grammar ───

def test_subscript_needs_migration_but_runs():
    cls, _ = classify_expression("items[0]")
    assert cls is Classification.NEEDS_MIGRATION
    assert evaluate("items[0]", {"items": [99]}) == 99


def test_unknown_call_needs_migration():
    # round() with a keyword is outside the strict bare-call grammar.
    cls, _ = classify_expression("round(x, ndigits=1)")
    assert cls is Classification.NEEDS_MIGRATION
    assert evaluate("round(x, ndigits=1)", {"x": 3.14159}) == 3.1


def test_needs_migration_cannot_escape():
    # Even on the hardened fallback, no builtins beyond the safe set exist.
    assert evaluate("range(10)", {}) is None  # range not whitelisted -> None


# ─── Resource caps ───────────────────────────────────────────────

def test_node_cap_rejects_huge_expression():
    expr = "+".join(["1"] * (MAX_NODES + 50))
    cls, reason = classify_expression(expr)
    assert cls is Classification.REJECTED
    assert "node" in reason or "large" in reason


def test_depth_cap_rejects_deeply_nested():
    # Parens collapse to one node; chained unary ops actually grow AST depth.
    expr = "not " * (MAX_DEPTH + 5) + "x"
    cls, reason = classify_expression(expr)
    assert cls is Classification.REJECTED, reason
    assert "nested" in reason or "depth" in reason


# ─── Failure contract ────────────────────────────────────────────

def test_unknown_name_returns_none():
    assert evaluate("missing + 1", {}) is None


def test_syntax_error_rejected():
    cls, _ = classify_expression("a +")
    assert cls is Classification.REJECTED
    assert evaluate("a +", {}) is None


def test_empty_expression_rejected():
    assert classify_expression("")[0] is Classification.REJECTED
    assert classify_expression("   ")[0] is Classification.REJECTED


def test_division_by_zero_returns_none():
    assert evaluate("a / b", {"a": 1, "b": 0}) is None
