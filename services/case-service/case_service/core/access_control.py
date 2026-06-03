"""Attribute-Based Access Control (ABAC) engine.

Evaluates SecurityProfile policies against user attributes and
resource context. Used as a FastAPI dependency for route-level
authorization.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import operator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UserContext:
    """Attributes of the current authenticated user."""

    user_id: str
    roles: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    department: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResourceContext:
    """Attributes of the resource being accessed."""

    resource_type: str  # "case", "case_type", "assignment", etc.
    resource_id: str | None = None
    owner_id: str | None = None
    case_type_id: str | None = None
    status: str | None = None
    priority: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccessDecision:
    """Result of an ABAC policy evaluation."""

    allowed: bool
    reason: str = ""
    matched_policy: str | None = None
    evaluated_policies: int = 0


# ─── Condition operators ──────────────────────────────────────────

_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "contains": lambda a, b: b in a if isinstance(a, (list, str)) else False,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "exists": lambda a, _: a is not None,
    "not_exists": lambda a, _: a is None,
}


def _resolve_attribute(key: str, user: UserContext, resource: ResourceContext) -> Any:
    """Resolve a dotted attribute path from user or resource context."""
    if key.startswith("user."):
        attr = key[5:]
        if hasattr(user, attr):
            return getattr(user, attr)
        return user.attributes.get(attr)
    elif key.startswith("resource."):
        attr = key[9:]
        if hasattr(resource, attr):
            return getattr(resource, attr)
        return resource.attributes.get(attr)
    elif key.startswith("env."):
        # Environment attributes (future: time of day, IP, etc.)
        return None
    return None


def evaluate_condition(
    condition: dict[str, Any],
    user: UserContext,
    resource: ResourceContext,
) -> bool:
    """Evaluate a single condition dict.

    Format: {"attribute": "user.role", "operator": "in", "value": ["admin", "manager"]}
    """
    attr_key = condition.get("attribute", "")
    op_name = condition.get("operator", "eq")
    expected = condition.get("value")

    actual = _resolve_attribute(attr_key, user, resource)
    op_fn = _OPS.get(op_name)
    if op_fn is None:
        logger.warning("Unknown ABAC operator: %s", op_name)
        return False

    try:
        return op_fn(actual, expected)
    except (TypeError, ValueError):
        return False


def evaluate_policy(
    policy: dict[str, Any],
    user: UserContext,
    resource: ResourceContext,
    required_permission: str,
) -> bool | None:
    """Evaluate a single access policy.

    Returns True if policy allows, False if denies, None if not applicable.
    """
    # Check if the policy covers this permission
    policy_permissions = policy.get("permissions", [])
    if required_permission not in policy_permissions and "*" not in policy_permissions:
        return None  # Not applicable

    # Evaluate all conditions (AND logic)
    conditions = policy.get("conditions", [])
    if isinstance(conditions, dict):
        # Legacy format: {"role": "admin"} → convert
        conditions = [
            {"attribute": f"user.{k}", "operator": "eq", "value": v}
            for k, v in conditions.items()
        ]

    if not conditions:
        # No conditions → policy always matches
        effect = policy.get("effect", "allow")
        return effect == "allow"

    all_match = all(
        evaluate_condition(c, user, resource) for c in conditions
    )
    if not all_match:
        return None  # Conditions didn't match

    effect = policy.get("effect", "allow")
    return effect == "allow"


def evaluate_access(
    security_profile: dict[str, Any],
    user: UserContext,
    resource: ResourceContext,
    required_permission: str,
) -> AccessDecision:
    """Evaluate all policies in a security profile.

    Policies are evaluated in priority order (highest first).
    First matching policy wins. If no policy matches, default deny.

    The security_profile dict should have:
    - roles: list of role definitions
    - policies: list of access policies
    """
    # Check role-based permissions first
    roles = security_profile.get("roles", [])
    user_role_ids = set(user.roles)

    role_permissions: set[str] = set()
    for role in roles:
        if role.get("id") in user_role_ids or role.get("name") in user_role_ids:
            role_permissions.update(role.get("permissions", []))

    # If user has permission via role and no ABAC policies exist, allow
    policies = security_profile.get("policies", [])
    if not policies and required_permission in role_permissions:
        return AccessDecision(
            allowed=True,
            reason="Granted via role permission",
            evaluated_policies=0,
        )

    # Evaluate ABAC policies in priority order
    sorted_policies = sorted(
        policies, key=lambda p: p.get("priority", 0), reverse=True
    )

    for policy in sorted_policies:
        result = evaluate_policy(policy, user, resource, required_permission)
        if result is not None:
            return AccessDecision(
                allowed=result,
                reason=f"Policy '{policy.get('name', policy.get('id', '?'))}' "
                       f"{'allowed' if result else 'denied'} access",
                matched_policy=policy.get("id"),
                evaluated_policies=len(sorted_policies),
            )

    # No policy matched — check role permissions as fallback
    if required_permission in role_permissions:
        return AccessDecision(
            allowed=True,
            reason="Granted via role permission (no ABAC policy matched)",
            evaluated_policies=len(sorted_policies),
        )

    # Default deny
    return AccessDecision(
        allowed=False,
        reason="No matching policy found — default deny",
        evaluated_policies=len(sorted_policies),
    )


def get_field_access(
    security_profile: dict[str, Any],
    user: UserContext,
) -> dict[str, dict[str, bool]]:
    """Compute per-field read/write/mask permissions for a user.

    Returns: { field_id: { readable, writable, masked } }
    """
    field_access = security_profile.get("field_access", [])
    result: dict[str, dict[str, bool]] = {}

    user_role_ids = set(user.roles)

    for fa in field_access:
        fid = fa.get("field_id", "")
        role_id = fa.get("role_id")

        # If role-restricted and user doesn't have the role, skip
        if role_id and role_id not in user_role_ids:
            continue

        # Merge — most permissive wins for readable/writable
        existing = result.get(fid, {"readable": False, "writable": False, "masked": True})
        result[fid] = {
            "readable": existing["readable"] or fa.get("readable", True),
            "writable": existing["writable"] or fa.get("writable", False),
            "masked": existing["masked"] and fa.get("masked", False),
        }

    return result
