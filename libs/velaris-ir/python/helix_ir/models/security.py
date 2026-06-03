"""HELIX IR: Security and access control models.

Role-based (RBAC) and attribute-based (ABAC) access control.
Evaluated at runtime by the auth protocol implementation.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Permission(enum.Enum):
    """Granular permission tokens used in access policies."""

    # Case-level
    CASE_CREATE = "case:create"
    CASE_READ = "case:read"
    CASE_UPDATE = "case:update"
    CASE_DELETE = "case:delete"
    CASE_ASSIGN = "case:assign"
    CASE_REASSIGN = "case:reassign"
    CASE_TRANSFER = "case:transfer"
    CASE_CLOSE = "case:close"
    CASE_REOPEN = "case:reopen"
    CASE_CANCEL = "case:cancel"
    # Data-level
    DATA_READ = "data:read"
    DATA_WRITE = "data:write"
    DATA_READ_PII = "data:read_pii"
    DATA_EXPORT = "data:export"
    # Process-level
    PROCESS_DEPLOY = "process:deploy"
    PROCESS_START = "process:start"
    PROCESS_CANCEL = "process:cancel"
    # Admin
    ADMIN_CONFIGURE = "admin:configure"
    ADMIN_AUDIT = "admin:audit"
    ADMIN_MANAGE_USERS = "admin:manage_users"
    ADMIN_MANAGE_QUEUES = "admin:manage_queues"


class AccessEffect(enum.Enum):
    """Whether a matched policy allows or denies access."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class RoleDefinition:
    """A named collection of permissions."""

    id: str
    name: str
    permissions: list[Permission] = field(default_factory=list)
    description: str = ""
    parent_role_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccessPolicy:
    """Attribute-based access policy.

    When all ``conditions`` match, ``effect`` is applied.
    Evaluated in priority order; first match wins.
    """

    id: str
    name: str
    effect: AccessEffect
    permissions: list[Permission]
    conditions: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    description: str = ""


@dataclass(frozen=True)
class FieldLevelAccess:
    """Per-field visibility and editability controls."""

    field_id: str
    role_id: str | None = None
    readable: bool = True
    writable: bool = False
    masked: bool = False
    condition: str | None = None


@dataclass
class SecurityProfile:
    """Complete security configuration for a case type or application."""

    id: str
    name: str
    roles: list[RoleDefinition] = field(default_factory=list)
    policies: list[AccessPolicy] = field(default_factory=list)
    field_access: list[FieldLevelAccess] = field(default_factory=list)
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
