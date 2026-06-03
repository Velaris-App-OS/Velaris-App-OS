"""Temporal activities for stage and step management.

These activities run inside the Temporal worker and call the
case-service REST API to perform database operations.  Activities
are the only place where I/O is allowed in Temporal.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from temporalio import activity

CASE_SERVICE_URL = os.environ.get(
    "HELIX_CASE_SERVICE_URL", "http://localhost:8200"
)


def _api(path: str) -> str:
    return f"{CASE_SERVICE_URL}/api/v1{path}"


@activity.defn
async def load_case_type_definition(
    case_type_id: str,
) -> dict[str, Any]:
    """Load the full case type definition from the case-service API."""
    activity.logger.info(
        "Loading case type definition: %s", case_type_id
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(_api(f"/case-types/{case_type_id}"))
        resp.raise_for_status()
        ct = resp.json()

    definition = ct.get("definition_json", {})
    return {
        "stages": definition.get("stages", []),
        "sla_policies": definition.get("sla_policies", []),
        "case_sla_policy_ids": [
            s["id"] for s in definition.get("sla_policies", [])
        ],
        "default_priority": ct.get("default_priority", "medium"),
        "name": ct.get("name", ""),
    }


@activity.defn
async def update_case_status(case_id: str, status: str) -> None:
    """Update the case instance status via the API."""
    activity.logger.info(
        "Updating case %s status to %s", case_id, status
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _api(f"/cases/{case_id}/status"),
            json={"status": status},
        )
        resp.raise_for_status()


@activity.defn
async def update_case_stage(case_id: str, stage_id: str) -> None:
    """Update the case's current_stage_id via the API."""
    activity.logger.info(
        "Case %s entering stage %s", case_id, stage_id
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _api(f"/cases/{case_id}/stage"),
            json={"target_stage_id": stage_id},
        )
        resp.raise_for_status()


@activity.defn
async def create_stage_assignments(
    case_id: str,
    stage_id: str,
    steps: list[dict[str, Any]],
) -> list[str]:
    """Create work-item assignments for each step in the stage.

    Calls the internal assignment creation endpoint for each step.
    Returns a list of created assignment IDs.
    """
    activity.logger.info(
        "Creating %d assignments for case %s stage %s",
        len(steps),
        case_id,
        stage_id,
    )
    assignment_ids: list[str] = []

    async with httpx.AsyncClient() as client:
        for step in steps:
            # Determine assignment target from step definition
            assignment = step.get("assignment", {}) or {}
            strategy = assignment.get("strategy", "queue_based")
            target = assignment.get("target", "default-queue")

            # Use the internal assignment creation
            # (The case-service creates assignments via repository)
            resp = await client.post(
                _api(f"/cases/{case_id}/assignments"),
                json={
                    "step_id": step["id"],
                    "assignee_type": _strategy_to_type(strategy),
                    "assignee_id": target,
                },
            )
            if resp.status_code == 201:
                assignment_ids.append(resp.json().get("id", ""))
            else:
                activity.logger.warning(
                    "Failed to create assignment for step %s: %s",
                    step["id"],
                    resp.text,
                )

    return assignment_ids


@activity.defn
async def evaluate_exit_criteria(
    case_id: str, stage_id: str
) -> bool:
    """Evaluate the stage's exit criteria.

    Checks if all required step assignments for this stage are completed.
    Returns True if the stage can be exited.
    """
    activity.logger.info(
        "Evaluating exit criteria for case %s stage %s",
        case_id,
        stage_id,
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _api(f"/cases/{case_id}/assignments"),
        )
        if resp.status_code != 200:
            return True  # If we can't check, allow exit

        assignments = resp.json()
        # Check if all active assignments are completed
        active = [a for a in assignments if a.get("status") == "active"]
        return len(active) == 0


@activity.defn
async def resolve_case(case_id: str) -> None:
    """Resolve the case when all stages are complete."""
    activity.logger.info("Resolving case %s", case_id)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _api(f"/cases/{case_id}/resolve"),
            json={},
        )
        resp.raise_for_status()


def _strategy_to_type(strategy: str) -> str:
    """Map assignment strategy to assignee_type."""
    if strategy in ("specific_user", "round_robin", "least_loaded", "manager_of"):
        return "user"
    elif strategy in ("role_based", "skill_based"):
        return "role"
    else:
        return "queue"
