"""Rules evaluator: default implementation of the RulesEngine protocol.

Evaluates business rules against a context dict.  Supports:
- WHEN rules (condition list → action list)
- Decision tables (multi-column condition→outcome lookup)
- Expressions (simple Python-like eval with sandboxing)
- Validation rules (data model field checks)
- Urgency computation (delegates to urgency_calculator)

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import operator
import re
from typing import Any

logger = logging.getLogger(__name__)


# ─── Operator registry ────────────────────────────────────────────

_OPS: dict[str, Any] = {
    "eq": operator.eq,
    "neq": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "in": lambda val, collection: val in collection,
    "not_in": lambda val, collection: val not in collection,
    "contains": lambda haystack, needle: needle in haystack,
    "not_contains": lambda haystack, needle: needle not in haystack,
    "starts_with": lambda val, prefix: str(val).startswith(str(prefix)),
    "ends_with": lambda val, suffix: str(val).endswith(str(suffix)),
    "is_empty": lambda val, _: val is None or val == "" or val == [],
    "is_not_empty": lambda val, _: val is not None and val != "" and val != [],
    "matches": lambda val, pattern: bool(re.match(str(pattern), str(val))),
    "between": lambda val, bounds: bounds[0] <= val <= bounds[1],
}


# ─── Context resolver ────────────────────────────────────────────


def resolve_path(context: dict[str, Any], path: str) -> Any:
    """Resolve a dot-notation path against a nested dict.

    Example: ``resolve_path({"case": {"data": {"amount": 500}}}, "case.data.amount")``
    returns ``500``.

    Case variables are flat dotted keys ("crm.account_status") inside the
    dict the case_vars façade returns — at every level the remaining path is
    first tried as ONE flat key before descending, so both
    ``case.data.crm.account_status`` and bare ``crm.account_status`` resolve.

    Returns ``None`` if the path doesn't exist.
    """
    parts = path.split(".")
    current = context
    for i, part in enumerate(parts):
        if not isinstance(current, dict):
            return None
        remainder = ".".join(parts[i:])
        if remainder != part and remainder in current:
            return current[remainder]
        current = current.get(part)
        if current is None:
            return None
    return current


# ─── Condition evaluation ─────────────────────────────────────────


def evaluate_condition(
    condition: dict[str, Any], context: dict[str, Any]
) -> bool:
    """Evaluate a single RuleCondition dict against context.

    Condition dict keys: ``field_path``, ``operator``, ``value``,
    optionally ``value_field_path``.
    """
    field_path = condition.get("field_path", "")
    op_name = condition.get("operator", "eq")
    actual_value = resolve_path(context, field_path)

    # Compare against another field or a literal
    if condition.get("value_field_path"):
        expected_value = resolve_path(context, condition["value_field_path"])
    else:
        expected_value = condition.get("value")

    op_fn = _OPS.get(op_name)
    if op_fn is None:
        logger.warning("Unknown operator: %s", op_name)
        return False

    try:
        return bool(op_fn(actual_value, expected_value))
    except (TypeError, ValueError) as e:
        logger.debug(
            "Condition eval error: %s %s %s → %s",
            actual_value, op_name, expected_value, e,
        )
        return False


def evaluate_conditions(
    conditions: list[dict[str, Any]], context: dict[str, Any]
) -> bool:
    """Evaluate a list of conditions (AND logic — all must pass)."""
    if not conditions:
        return True
    return all(evaluate_condition(c, context) for c in conditions)


# ─── Action execution ─────────────────────────────────────────────


def execute_action(
    action: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Execute a single RuleAction dict.

    Returns a result dict describing what was done.
    Mutates ``context`` for ``set_value`` actions.
    """
    action_type = action.get("action_type", "")
    target = action.get("target")
    value = action.get("value")
    params = action.get("parameters", {})

    if action_type == "set_value" and target:
        _set_path(context, target, value)
        return {"action": "set_value", "target": target, "value": value}

    elif action_type == "raise_error":
        return {
            "action": "raise_error",
            "message": value or params.get("message", "Rule error"),
        }

    elif action_type == "log":
        logger.info("Rule log: %s", value)
        return {"action": "log", "message": value}

    else:
        # Actions like send_notification, assign_to, create_subcase
        # are returned as descriptors for the caller to execute
        return {
            "action": action_type,
            "target": target,
            "value": value,
            "parameters": params,
        }


def execute_actions(
    actions: list[dict[str, Any]], context: dict[str, Any]
) -> list[dict[str, Any]]:
    """Execute a list of actions, returning results for each."""
    return [execute_action(a, context) for a in actions]


def _set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    """Set a value at a dot-notation path, creating intermediate dicts."""
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


# ─── WHEN rule evaluation ─────────────────────────────────────────


def evaluate_when_rule(
    rule: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate a WHEN rule: if all conditions match, execute actions.

    Returns::

        {
            "matched": bool,
            "rule_id": str,
            "action_results": [...] or None
        }
    """
    conditions = rule.get("conditions", [])
    matched = evaluate_conditions(conditions, context)

    result = {
        "matched": matched,
        "rule_id": rule.get("id", ""),
        "rule_name": rule.get("name", ""),
        "action_results": None,
    }

    if matched:
        actions = rule.get("actions", [])
        result["action_results"] = execute_actions(actions, context)

    return result


# ─── Decision table evaluation ────────────────────────────────────


def evaluate_decision_table(
    rule: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate a decision table rule.

    Iterates rows in priority order, returns the first matching row's
    outcomes.  If no row matches, returns empty outcomes.

    Returns::

        {
            "matched": bool,
            "rule_id": str,
            "outcomes": dict,
            "matched_row_index": int or None
        }
    """
    columns = {c["id"]: c for c in rule.get("table_columns", [])}
    rows = sorted(
        rule.get("table_rows", []),
        key=lambda r: r.get("priority", 0),
        reverse=True,
    )

    for idx, row in enumerate(rows):
        row_matches = True
        for col_id, expected in row.get("conditions", {}).items():
            col = columns.get(col_id)
            if col is None or not col.get("is_condition", True):
                continue
            field_path = col.get("field_path", "")
            actual = resolve_path(context, field_path)

            # Support range conditions {"$gte": 100, "$lt": 500}
            if isinstance(expected, dict):
                for range_op, range_val in expected.items():
                    op_name = range_op.lstrip("$")
                    op_fn = _OPS.get(op_name)
                    if op_fn and not op_fn(actual, range_val):
                        row_matches = False
                        break
            elif actual != expected:
                row_matches = False

            if not row_matches:
                break

        if row_matches:
            # Resolve outcome values
            outcomes = {}
            for col_id, value in row.get("outcomes", {}).items():
                col = columns.get(col_id)
                if col:
                    outcomes[col.get("field_path", col_id)] = value
            return {
                "matched": True,
                "rule_id": rule.get("id", ""),
                "outcomes": outcomes,
                "matched_row_index": idx,
            }

    return {
        "matched": False,
        "rule_id": rule.get("id", ""),
        "outcomes": {},
        "matched_row_index": None,
    }


# ─── Expression evaluation ────────────────────────────────────────

# Allowed builtins for safe expression evaluation
def evaluate_expression(
    expression: str, context: dict[str, Any]
) -> Any:
    """Evaluate a simple expression against context.

    Uses a restricted eval with only safe builtins and the
    flattened context as local variables.

    Examples::

        evaluate_expression("case_data_amount * 0.1", {"case_data_amount": 500})
        # → 50.0

        evaluate_expression("priority_value + sla_factor", {...})
    """
    # Flatten context: "case.data.amount" → "case_data_amount"
    flat = {}
    _flatten_dict(context, "", flat)

    # HxSandbox #17 Phase 1: customer-authored expressions no longer hit a
    # restricted ``eval`` directly. safe_expression classifies the AST,
    # forbids attribute/dunder access (the sandbox-escape vector), and runs
    # CONFORMING expressions on a pure-Python walker. See safe_expression.py.
    from case_service.core.safe_expression import evaluate as _safe_evaluate
    return _safe_evaluate(expression, flat)


def _flatten_dict(
    d: dict[str, Any], prefix: str, out: dict[str, Any]
) -> None:
    """Flatten a nested dict: {"a": {"b": 1}} → {"a_b": 1}.

    Dotted keys (flat namespaced case variables like "crm.account_status")
    are emitted with dots mapped to underscores so expressions can reference
    them as valid identifiers: ``crm_account_status`` /
    ``case_data_crm_account_status``.

    Collision rule: dotted keys are emitted LAST, so a typed namespaced
    variable always wins over a same-named bare key ("crm_account_status"
    in the blob is caller-controlled; "crm.account_status" is provenance-
    tracked — the trusted one must not be shadowable).
    """
    plain = [(k, v) for k, v in d.items() if "." not in k]
    dotted = [(k, v) for k, v in d.items() if "." in k]
    for k, v in plain + dotted:
        k = k.replace(".", "_")
        key = f"{prefix}{k}" if not prefix else f"{prefix}_{k}"
        if isinstance(v, dict):
            _flatten_dict(v, key, out)
        else:
            out[key] = v


# ─── Data validation ──────────────────────────────────────────────


def validate_field(
    field_def: dict[str, Any], value: Any
) -> list[dict[str, str]]:
    """Validate a single field value against its FieldDefinition.

    Returns a list of error dicts (empty if valid).
    """
    errors = []
    field_name = field_def.get("name", "unknown")
    validations = field_def.get("validations", [])

    for v in validations:
        rule = v.get("rule", "")
        constraint = v.get("value")
        message = v.get("message", "")

        if rule == "required" and (value is None or value == ""):
            errors.append({
                "field": field_name,
                "rule": "required",
                "message": message or f"{field_name} is required",
            })

        elif rule == "min_length" and isinstance(value, str):
            if len(value) < (constraint or 0):
                errors.append({
                    "field": field_name,
                    "rule": "min_length",
                    "message": message or f"{field_name} must be at least {constraint} characters",
                })

        elif rule == "max_length" and isinstance(value, str):
            if len(value) > (constraint or float("inf")):
                errors.append({
                    "field": field_name,
                    "rule": "max_length",
                    "message": message or f"{field_name} must be at most {constraint} characters",
                })

        elif rule == "min_value" and value is not None:
            try:
                if float(value) < float(constraint or 0):
                    errors.append({
                        "field": field_name,
                        "rule": "min_value",
                        "message": message or f"{field_name} must be at least {constraint}",
                    })
            except (TypeError, ValueError):
                pass

        elif rule == "max_value" and value is not None:
            try:
                if float(value) > float(constraint or float("inf")):
                    errors.append({
                        "field": field_name,
                        "rule": "max_value",
                        "message": message or f"{field_name} must be at most {constraint}",
                    })
            except (TypeError, ValueError):
                pass

        elif rule == "pattern" and isinstance(value, str) and constraint:
            if not re.match(constraint, value):
                errors.append({
                    "field": field_name,
                    "rule": "pattern",
                    "message": message or f"{field_name} does not match pattern {constraint}",
                })

    return errors


def validate_data(
    data_model: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    """Validate a data dict against a DataModelDefinition.

    Returns::

        {
            "valid": bool,
            "errors": [{"field": ..., "rule": ..., "message": ...}, ...],
            "field_count": int,
            "validated_count": int
        }
    """
    all_errors = []
    fields = data_model.get("fields", [])

    for field_def in fields:
        field_name = field_def.get("name", "")
        value = data.get(field_name)
        field_errors = validate_field(field_def, value)
        all_errors.extend(field_errors)

    return {
        "valid": len(all_errors) == 0,
        "errors": all_errors,
        "field_count": len(fields),
        "validated_count": len(fields),
    }


# ─── Top-level evaluate function ──────────────────────────────────


def evaluate_rule(
    rule: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate any rule type based on its ``rule_type`` field.

    Dispatches to the appropriate evaluator.
    """
    rule_type = rule.get("rule_type", "")

    if rule_type == "when":
        return evaluate_when_rule(rule, context)

    elif rule_type in ("decision_table", "decision_tree"):
        return evaluate_decision_table(rule, context)

    elif rule_type in ("expression", "declare_expression"):
        expression = rule.get("expression", "")
        result_path = rule.get("result_field_path")
        value = evaluate_expression(expression, context)
        if result_path and value is not None:
            _set_path(context, result_path, value)
        return {
            "rule_id": rule.get("id", ""),
            "rule_type": rule_type,
            "result": value,
            "result_field_path": result_path,
        }

    elif rule_type == "validation":
        # Treat conditions as validation checks
        conditions = rule.get("conditions", [])
        errors = []
        for c in conditions:
            if not evaluate_condition(c, context):
                errors.append({
                    "field": c.get("field_path", ""),
                    "operator": c.get("operator", ""),
                    "message": f"Validation failed: {c.get('field_path')} {c.get('operator')} {c.get('value')}",
                })
        return {
            "rule_id": rule.get("id", ""),
            "valid": len(errors) == 0,
            "errors": errors,
        }

    elif rule_type == "routing":
        result = evaluate_when_rule(rule, context)
        return {**result, "rule_type": "routing"}

    elif rule_type == "constraint":
        conditions = rule.get("conditions", [])
        holds = evaluate_conditions(conditions, context)
        return {
            "rule_id": rule.get("id", ""),
            "rule_type": "constraint",
            "holds": holds,
        }

    else:
        return {
            "rule_id": rule.get("id", ""),
            "error": f"Unknown rule type: {rule_type}",
        }


def evaluate_rules(
    rules: list[dict[str, Any]], context: dict[str, Any]
) -> list[dict[str, Any]]:
    """Evaluate multiple rules in priority order.

    Rules are sorted by ``priority`` (descending) before evaluation.
    """
    sorted_rules = sorted(
        rules,
        key=lambda r: r.get("priority", 0),
        reverse=True,
    )
    return [evaluate_rule(r, context) for r in sorted_rules]
