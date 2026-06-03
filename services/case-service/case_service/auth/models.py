"""Auth data models.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActiveAccessGroup:
    """The operator's currently active access group context (P37)."""
    id: str
    name: str
    portal_id: str
    portal_type: str
    portal_name: str
    modules: list[str]
    homepage: str
    roles: list[str]         # role names — used to satisfy require_role() checks
    privileges: list[dict]   # raw ABAC privilege objects for evaluate_access()
    allowed_case_type_ids: list[str]
    allowed_queue_ids: list[str]


@dataclass
class AuthenticatedUser:
    """Represents the current authenticated user.

    roles is populated from BOTH the legacy flat field (backwards compat)
    AND the active access group's role names (P37). require_role() keeps
    working unchanged for all existing routes.
    """
    user_id: str
    username: str = ""
    email: str = ""
    roles: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    department: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    token: str = ""
    tenant_id: str | None = None          # owning tenant (from user_directory)
    # P37 — None when operator has no access group assigned yet
    active_access_group: ActiveAccessGroup | None = None
    available_access_groups: list[dict] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def is_designer(self) -> bool:
        return "designer" in self.roles or self.is_admin

    @property
    def is_case_worker(self) -> bool:
        return "case_worker" in self.roles or self.is_admin

    def has_role(self, role: str) -> bool:
        return role in self.roles or "admin" in self.roles

    def has_privilege(self, resource: str, action: str) -> bool:
        """Check whether the user's active access group grants a specific action
        on a resource, based on the privileges configured in the Access Directory.

        Privilege entries look like:
            {"resource": "case_type", "actions": ["create", "update"]}
            {"resource": "*",         "actions": ["*"]}   ← wildcard (admin)

        Falls back to True for superadmin. Falls back to role-name check when
        no access group is assigned (pre-P37 compatibility).
        """
        if "superadmin" in (self.roles or []):
            return True

        ag = self.active_access_group
        if ag is None:
            # No access group assigned — fall back to legacy is_admin check
            return self.is_admin

        for priv in (ag.privileges or []):
            priv_resource = priv.get("resource", "")
            priv_actions = priv.get("actions", [])
            resource_match = priv_resource in ("*", resource)
            action_match = "*" in priv_actions or action in priv_actions
            if resource_match and action_match:
                return True

        return False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "roles": self.roles,
            "groups": self.groups,
            "department": self.department,
            "is_admin": self.is_admin,
            "is_designer": self.is_designer,
            "is_case_worker": self.is_case_worker,
        }
        if self.active_access_group:
            ag = self.active_access_group
            d["active_access_group"] = {
                "id": ag.id,
                "name": ag.name,
                "portal": {
                    "id": ag.portal_id,
                    "name": ag.portal_name,
                    "portal_type": ag.portal_type,
                    "modules": ag.modules,
                    "homepage": ag.homepage,
                },
                "roles": ag.roles,
                "privileges": ag.privileges,
                "allowed_case_type_ids": ag.allowed_case_type_ids,
                "allowed_queue_ids": ag.allowed_queue_ids,
            }
        d["available_access_groups"] = self.available_access_groups
        return d
