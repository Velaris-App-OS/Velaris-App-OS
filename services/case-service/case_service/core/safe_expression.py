"""Safe expression evaluation (HxSandbox #17, Phase 1).

Replaces the restricted ``eval`` that ``rules_evaluator.evaluate_expression``
used to run on customer-authored expression strings. Restricted-builtins
``eval`` is a classic sandbox escape: attribute traversal
(``().__class__.__bases__[0].__subclasses__()…``) reaches ``os``, the DB
engine, OpenBao-rendered secrets, and other tenants' data. The context that
expression rules run against is *flattened to bare identifiers*
(``crm_account_status``), so no legitimate rule needs attribute access — and
forbidding ``Attribute`` nodes is what makes the escape airtight.

Three classifications (see :class:`Classification`):

* ``CONFORMING`` — within the strict safe grammar. Evaluated by the
  hand-rolled :func:`_eval_node` walker, with **no** ``eval`` involved.
* ``NEEDS_MIGRATION`` — contains no ``Attribute``/dunder access (so it is
  eval-safe) but uses constructs outside the strict grammar. Kept running on
  the hardened restricted-``eval`` path during the deprecation window
  (roadmap §6.2/§7.4); flagged for migration by the lint pass.
* ``REJECTED`` — contains an ``Attribute`` node, a dunder identifier, an
  import, or exceeds the node/depth caps. Never evaluated; returns ``None``.

New rules are validated at create/update time and must be ``CONFORMING`` —
so attacker-supplied input never reaches the ``eval`` fallback at all.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ast
import logging
import operator
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ─── Resource bounds ──────────────────────────────────────────────
# Cap the parsed AST so a pathological expression can't burn CPU/memory
# during the walk. Real metering arrives with the WASM host (Phase 2).
MAX_NODES = 250
MAX_DEPTH = 30


# ─── Whitelisted callables ────────────────────────────────────────
# Mirrors the historical ``_SAFE_BUILTINS`` in rules_evaluator — the only
# functions a CONFORMING expression may call.
_SAFE_CALLS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "sum": sum,
    "any": any,
    "all": all,
}


# ─── Allowed AST node types for the STRICT grammar ────────────────
_STRICT_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_STRICT_UNARYOPS = (ast.UAdd, ast.USub, ast.Not)
_STRICT_CMPOPS = (
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)
_STRICT_BOOLOPS = (ast.And, ast.Or)

# Node *types* permitted in a CONFORMING expression. Calls get an extra
# check (callee must be a bare Name in _SAFE_CALLS) in the classifier.
_STRICT_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    *_STRICT_BINOPS,
    *_STRICT_UNARYOPS,
    *_STRICT_CMPOPS,
    *_STRICT_BOOLOPS,
)


# ─── Operator dispatch for the pure-Python walker ─────────────────
_BINOP_FN: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOP_FN: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}
_CMPOP_FN: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


class Classification(str, Enum):
    """How an expression is treated by the safe evaluator."""

    CONFORMING = "conforming"
    NEEDS_MIGRATION = "needs_migration"
    REJECTED = "rejected"


class ExpressionError(Exception):
    """Raised by the pure walker on an unrepresentable / unsafe construct."""


# ─── Classification ───────────────────────────────────────────────


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def classify_expression(expression: str) -> tuple[Classification, str]:
    """Classify *expression* without evaluating it.

    Returns ``(classification, reason)``. ``reason`` is a human-readable
    explanation suitable for create/update error messages and the lint
    report; it is empty for ``CONFORMING``.
    """
    if not isinstance(expression, str) or not expression.strip():
        return Classification.REJECTED, "empty expression"

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        return Classification.REJECTED, f"syntax error: {e.msg}"

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        return Classification.REJECTED, f"too large ({len(nodes)} nodes > {MAX_NODES})"
    depth = _max_depth(tree)
    if depth > MAX_DEPTH:
        return Classification.REJECTED, f"too deeply nested (depth {depth} > {MAX_DEPTH})"

    strict = True
    for node in nodes:
        # ── Hard rejects: the escape vectors ──
        if isinstance(node, ast.Attribute):
            return Classification.REJECTED, "attribute access is not allowed"
        if isinstance(node, ast.Name) and _is_dunder(node.id):
            return Classification.REJECTED, f"dunder identifier '{node.id}' is not allowed"
        if isinstance(node, (ast.Lambda, ast.Await, ast.Yield, ast.YieldFrom,
                             ast.NamedExpr)):
            return Classification.REJECTED, f"'{type(node).__name__}' is not allowed"

        # ── Strict-grammar membership ──
        if isinstance(node, ast.Call):
            # Only bare-Name calls to whitelisted builtins are strict.
            if not (isinstance(node.func, ast.Name)
                    and node.func.id in _SAFE_CALLS
                    and not node.keywords):
                strict = False
        elif not isinstance(node, _STRICT_NODES):
            strict = False

    if strict:
        return Classification.CONFORMING, ""
    return (
        Classification.NEEDS_MIGRATION,
        "uses constructs outside the strict grammar; runs on the hardened "
        "fallback during the deprecation window",
    )


def _max_depth(node: ast.AST, _d: int = 0) -> int:
    children = list(ast.iter_child_nodes(node))
    if not children:
        return _d
    return max(_max_depth(c, _d + 1) for c in children)


# ─── Pure-Python evaluation of CONFORMING expressions ─────────────


def _eval_node(node: ast.AST, names: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, names)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise ExpressionError(f"unknown name '{node.id}'")
    if isinstance(node, ast.BinOp):
        fn = _BINOP_FN.get(type(node.op))
        if fn is None:
            raise ExpressionError(f"operator {type(node.op).__name__} not allowed")
        return fn(_eval_node(node.left, names), _eval_node(node.right, names))
    if isinstance(node, ast.UnaryOp):
        fn = _UNARYOP_FN.get(type(node.op))
        if fn is None:
            raise ExpressionError(f"operator {type(node.op).__name__} not allowed")
        return fn(_eval_node(node.operand, names))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval_node(v, names)
                if not result:
                    return result
            return result
        # Or
        result = False
        for v in node.values:
            result = _eval_node(v, names)
            if result:
                return result
        return result
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, names)
        for op, comparator in zip(node.ops, node.comparators):
            fn = _CMPOP_FN.get(type(op))
            if fn is None:
                raise ExpressionError(f"comparison {type(op).__name__} not allowed")
            right = _eval_node(comparator, names)
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        if _eval_node(node.test, names):
            return _eval_node(node.body, names)
        return _eval_node(node.orelse, names)
    if isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name) and node.func.id in _SAFE_CALLS):
            raise ExpressionError("only whitelisted builtin calls are allowed")
        args = [_eval_node(a, names) for a in node.args]
        return _SAFE_CALLS[node.func.id](*args)
    raise ExpressionError(f"node {type(node).__name__} not allowed")


# ─── Public entry point ───────────────────────────────────────────


def evaluate(expression: str, names: dict[str, Any]) -> Any:
    """Safely evaluate *expression* against the flattened *names* mapping.

    Returns the computed value, or ``None`` on any failure (mirrors the
    historical ``evaluate_expression`` contract). CONFORMING expressions run
    on the pure-Python walker; NEEDS_MIGRATION ones run on the hardened
    restricted ``eval`` (attribute- and dunder-free, verified by the
    classifier); REJECTED ones are never evaluated.
    """
    classification, reason = classify_expression(expression)

    if classification is Classification.REJECTED:
        logger.warning("Expression rejected (%s): %s", reason, expression)
        return None

    try:
        if classification is Classification.CONFORMING:
            tree = ast.parse(expression, mode="eval")
            return _eval_node(tree, names)
        # NEEDS_MIGRATION — hardened fallback. The classifier has proven the
        # AST is free of Attribute nodes and dunder identifiers, so the only
        # reachable objects are the flattened context values and the safe
        # builtins. This bridge runs only for pre-existing rules; new rules
        # must be CONFORMING (enforced at create/update).
        logger.info("Expression on hardened fallback (needs migration): %s", expression)
        return eval(expression, {"__builtins__": dict(_SAFE_CALLS)}, names)  # noqa: S307
    except Exception as e:  # noqa: BLE001 — safe failure is the contract
        logger.warning("Expression eval error: %s → %s", expression, e)
        return None
