"""
Gateway Execution Handlers
===========================

Each gateway type gets its own handler function.  The runtime calls the
right one based on the element type (``match`` / ``isinstance``).

Responsibilities:
  - **Exclusive**: Evaluate conditions, pick ONE branch.
  - **Parallel**: Fork into ALL branches, join all results.
  - **Inclusive**: Fork into branches whose conditions are true.
  - **Event-based**: Delegate to the event system (first-wins).

All handlers share the same signature::

    async def handle_*(
        gateway,           # The IR gateway element
        process,           # The BPMNProcess IR
        variables,         # Current process variables
        condition_eval,    # Pluggable condition evaluator
    ) -> GatewayResult

This makes them easy to test individually — no framework, no runtime needed.
"""

from __future__ import annotations

import ast as _ast
from dataclasses import dataclass, field

import structlog

from helix_ir.models.process import (
    BPMNProcess,
    EventBasedGateway,
    ExclusiveGateway,
    InclusiveGateway,
    ParallelGateway,
    SequenceFlow,
)
from helix_sdk.protocols.llm import LLMProvider  # noqa: F401 — future AI-based routing

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════════
#  Condition evaluator protocol (pluggable)
# ═══════════════════════════════════════════════════════════════════════

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ConditionEvaluator(Protocol):
    """
    Evaluates a condition expression against process variables.

    Plugins can implement this to support different expression languages
    (FEEL, JUEL, Python, etc.).
    """
    def evaluate(self, expression: str, variables: dict[str, Any]) -> bool: ...


# Explicitly whitelisted AST node types for condition expressions.
# Any node type NOT in this set causes the expression to be rejected.
# This defeats the classic {"__builtins__": {}} bypass via
#   ().__class__.__mro__[1].__subclasses__()
# because ast.Attribute is absent from the whitelist.
_SAFE_NODES: frozenset = frozenset(filter(None, {
    _ast.Expression,
    # Literals
    _ast.Constant,
    # Variable references resolved from the process-variables dict
    _ast.Name, _ast.Load,
    # Subscript — e.g. items['key'] or items[0]
    _ast.Subscript,
    getattr(_ast, "Index", None),   # ast.Index removed in Python 3.9
    # Boolean operators
    _ast.BoolOp, _ast.And, _ast.Or,
    # Unary operators
    _ast.UnaryOp, _ast.Not, _ast.USub, _ast.UAdd,
    # Arithmetic
    _ast.BinOp,
    _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.Mod, _ast.FloorDiv,
    # Comparisons
    _ast.Compare,
    _ast.Eq, _ast.NotEq, _ast.Lt, _ast.LtE, _ast.Gt, _ast.GtE,
    _ast.In, _ast.NotIn, _ast.Is, _ast.IsNot,
    # Ternary: x if cond else y (all parts are checked recursively)
    _ast.IfExp,
}))


class _UnsafeExpressionError(ValueError):
    pass


def _assert_ast_safe(node: _ast.AST) -> None:
    """Recursively reject any AST node not in the safe-node whitelist."""
    if type(node) not in _SAFE_NODES:
        raise _UnsafeExpressionError(f"Forbidden expression construct: {type(node).__name__}")
    for child in _ast.iter_child_nodes(node):
        _assert_ast_safe(child)


class DefaultConditionEvaluator:
    """
    AST-validated condition evaluator.

    Parses the expression, walks every AST node against an explicit
    whitelist, then evaluates.  Rejects attribute access, calls, imports,
    comprehensions, f-strings, and any other construct that could escape
    the sandbox — regardless of what ``__builtins__`` is set to.

    Supports simple conditions such as ``amount > 1000``,
    ``status == 'approved'``, ``a and b``, ``items['key'] != None``.
    """

    def evaluate(self, expression: str, variables: dict[str, Any]) -> bool:
        if not expression or not expression.strip():
            return False
        try:
            tree = _ast.parse(expression.strip(), mode="eval")
            _assert_ast_safe(tree)
            safe_globals: dict[str, Any] = {"__builtins__": None}
            safe_locals: dict[str, Any] = {**variables, "True": True, "False": False, "None": None}
            return bool(eval(compile(tree, "<condition>", "eval"), safe_globals, safe_locals))
        except _UnsafeExpressionError as exc:
            logger.warning("condition_rejected_unsafe", expression=expression, reason=str(exc))
            return False
        except Exception as exc:
            logger.warning("condition_eval_failed", expression=expression, error=str(exc))
            return False


# ═══════════════════════════════════════════════════════════════════════
#  Gateway result
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class GatewayResult:
    """
    The output of a gateway handler.

    ``next_element_ids`` contains the element id(s) to execute next:
      - Exclusive: exactly one id
      - Parallel: all branch target ids
      - Inclusive: one or more branch target ids
    """
    next_element_ids: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════
#  Handlers — one per gateway type
# ═══════════════════════════════════════════════════════════════════════

async def handle_exclusive_gateway(
    gateway: ExclusiveGateway,
    process: BPMNProcess,
    variables: dict[str, Any],
    condition_eval: ConditionEvaluator | None = None,
) -> GatewayResult:
    """
    Evaluate conditions on outgoing flows and take exactly ONE branch.

    Evaluation order:
      1. Conditioned flows (in document order).
      2. Default flow (if no condition matched).
      3. First outgoing flow (last resort).
    """
    evaluator = condition_eval or DefaultConditionEvaluator()
    outgoing = process.outgoing_flows(gateway.id)

    # Try conditioned flows first
    for flow in outgoing:
        if flow.condition and flow.id != gateway.default_flow:
            if evaluator.evaluate(flow.condition, variables):
                logger.debug("exclusive_gateway_took_branch",
                              gateway=gateway.id, flow=flow.id)
                return GatewayResult(next_element_ids=[flow.target_ref])

    # Default flow
    if gateway.default_flow:
        default = process.flows.get(gateway.default_flow)
        if default:
            logger.debug("exclusive_gateway_took_default",
                          gateway=gateway.id, flow=default.id)
            return GatewayResult(next_element_ids=[default.target_ref])

    # Last resort: first outgoing flow
    if outgoing:
        logger.warning("exclusive_gateway_no_match_using_first",
                         gateway=gateway.id)
        return GatewayResult(next_element_ids=[outgoing[0].target_ref])

    logger.warning("exclusive_gateway_dead_end", gateway=gateway.id)
    return GatewayResult()


async def handle_parallel_gateway(
    gateway: ParallelGateway,
    process: BPMNProcess,
    variables: dict[str, Any],
    condition_eval: ConditionEvaluator | None = None,
) -> GatewayResult:
    """
    Activate ALL outgoing flows.  Conditions are ignored.

    The runtime is responsible for executing these branches concurrently
    and synchronising at the converging parallel gateway.
    """
    outgoing = process.outgoing_flows(gateway.id)
    targets = [flow.target_ref for flow in outgoing]

    logger.debug("parallel_gateway_forking",
                  gateway=gateway.id, branch_count=len(targets))
    return GatewayResult(next_element_ids=targets)


async def handle_inclusive_gateway(
    gateway: InclusiveGateway,
    process: BPMNProcess,
    variables: dict[str, Any],
    condition_eval: ConditionEvaluator | None = None,
) -> GatewayResult:
    """
    Activate all outgoing flows whose conditions evaluate to True.

    At least one branch must be taken — falls back to default if nothing matches.
    """
    evaluator = condition_eval or DefaultConditionEvaluator()
    outgoing = process.outgoing_flows(gateway.id)
    active_targets: list[str] = []

    for flow in outgoing:
        if flow.id == gateway.default_flow:
            continue  # Default is a fallback
        if flow.condition:
            if evaluator.evaluate(flow.condition, variables):
                active_targets.append(flow.target_ref)
        else:
            # Unconditional non-default flow — always taken
            active_targets.append(flow.target_ref)

    # Fallback to default if nothing matched
    if not active_targets and gateway.default_flow:
        default = process.flows.get(gateway.default_flow)
        if default:
            active_targets.append(default.target_ref)

    if not active_targets:
        logger.warning("inclusive_gateway_no_branch", gateway=gateway.id)

    logger.debug("inclusive_gateway_forking",
                  gateway=gateway.id, branch_count=len(active_targets))
    return GatewayResult(next_element_ids=active_targets)


async def handle_event_based_gateway(
    gateway: EventBasedGateway,
    process: BPMNProcess,
    variables: dict[str, Any],
    condition_eval: ConditionEvaluator | None = None,
) -> GatewayResult:
    """
    Event-based gateways wait for the first event to fire.

    In Temporal, this becomes a race between multiple signal/timer waits.
    The runtime's event handler determines which event fires first and
    returns only that branch.

    For now, we return ALL possible targets — the Temporal workflow layer
    handles the actual race condition.
    """
    outgoing = process.outgoing_flows(gateway.id)
    targets = [flow.target_ref for flow in outgoing]

    logger.debug("event_based_gateway_waiting",
                  gateway=gateway.id, candidate_count=len(targets))
    return GatewayResult(next_element_ids=targets)
