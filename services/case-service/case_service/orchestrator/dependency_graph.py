"""Dependency graph builder for migration planning.

Analyzes relationships between artifacts and produces a topological
ordering so dependencies can be migrated before their dependents.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any


# Types that are foundational and should migrate first
FOUNDATION_TYPES = {"data_model", "role"}
# Types that depend on others
DEPENDENT_TYPES = {"process", "case_type", "workflow", "user_interface", "form"}


def build_dependencies(artifacts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Build a dependency map: artifact_id → list of artifact_ids it depends on.

    Uses heuristic matching:
    - Forms depend on data models
    - Workflows depend on rules
    - Cases depend on case types, forms, and rules
    - Integrations are leaf nodes
    """
    # Group artifacts by type for quick lookup
    by_type: dict[str, list[dict]] = defaultdict(list)
    for a in artifacts:
        by_type[a.get("type", "unknown")].append(a)

    # Index all artifact names for matching
    artifact_names = {a["identifier"]: a for a in artifacts}

    deps: dict[str, list[str]] = {}

    for artifact in artifacts:
        aid = artifact["identifier"]
        atype = artifact.get("type", "unknown")
        name = artifact.get("name", "")
        deps[aid] = []

        # Heuristic: forms reference data models
        if atype == "form":
            for dm in by_type.get("data_model", []):
                if dm["name"].lower() in name.lower() or name.lower() in dm["name"].lower():
                    deps[aid].append(dm["identifier"])

        # Workflows depend on rules and decision tables
        if atype in ("workflow", "process"):
            for rule in by_type.get("rule", []) + by_type.get("decision_table", []):
                # Naive: if rule name appears in workflow name
                if rule["name"] in name:
                    deps[aid].append(rule["identifier"])

        # Case types depend on forms and workflows
        if atype == "case_type":
            for form in by_type.get("form", []):
                if form["name"].startswith(name) or name.startswith(form["name"]):
                    deps[aid].append(form["identifier"])
            for wf in by_type.get("workflow", []) + by_type.get("process", []):
                if wf["name"].startswith(name) or name in wf["name"]:
                    deps[aid].append(wf["identifier"])

        # UI / interfaces depend on case types and forms
        if atype == "user_interface":
            for ct in by_type.get("case_type", []):
                if ct["name"] in name or name in ct["name"]:
                    deps[aid].append(ct["identifier"])

        # Deduplicate
        deps[aid] = list(set(deps[aid]))

    return deps


def topological_sort(
    artifacts: list[dict[str, Any]],
    deps: dict[str, list[str]],
) -> list[str]:
    """Kahn's algorithm — returns artifact IDs in dependency order.

    Foundation artifacts (no deps) come first; highly-dependent ones come last.
    Cycles are broken silently (we just proceed).
    """
    in_degree: dict[str, int] = {a["identifier"]: 0 for a in artifacts}
    for deps_list in deps.values():
        for d in deps_list:
            if d in in_degree:
                in_degree[d] += 0  # won't change, but ensures key exists

    # Reverse: for each edge a→b, b appears earlier (b is a dep of a)
    # Count how many things depend on each artifact
    dependents: dict[str, list[str]] = defaultdict(list)
    for a_id, deps_list in deps.items():
        for d in deps_list:
            dependents[d].append(a_id)

    # in_degree[x] = number of deps x has
    for a_id, deps_list in deps.items():
        in_degree[a_id] = len([d for d in deps_list if d in in_degree])

    # Start with artifacts having no dependencies
    queue = deque([aid for aid, deg in in_degree.items() if deg == 0])
    ordered = []

    while queue:
        aid = queue.popleft()
        ordered.append(aid)
        for dependent in dependents.get(aid, []):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Any remaining = cycles; append anyway
    for aid, deg in in_degree.items():
        if aid not in ordered:
            ordered.append(aid)

    return ordered


def phase_for_compatibility(compatibility: str) -> int:
    """Map compatibility level to migration phase (1-4)."""
    return {
        "full": 1,
        "high": 2,
        "medium": 3,
        "low": 4,
        "incompatible": 4,
    }.get(compatibility, 3)
