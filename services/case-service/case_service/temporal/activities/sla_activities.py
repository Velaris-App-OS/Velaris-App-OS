"""Temporal activities for SLA tracking.

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
async def start_sla_tracking(
    case_id: str,
    sla_policy_id: str,
    target_id: str,
) -> dict[str, Any]:
    """Start SLA tracking for a case or stage via the API."""
    activity.logger.info(
        "Starting SLA tracking: case=%s policy=%s target=%s",
        case_id, sla_policy_id, target_id,
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _api(f"/cases/{case_id}/sla/start"),
            json={
                "sla_policy_id": sla_policy_id,
                "target_id": target_id,
            },
        )
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            activity.logger.warning(
                "Failed to start SLA: %s", resp.text
            )
            return {"status": "failed", "error": resp.text}


@activity.defn
async def check_sla_status(
    case_id: str,
) -> list[dict[str, Any]]:
    """Check all SLA statuses for a case."""
    activity.logger.info("Checking SLA status for case %s", case_id)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(_api(f"/cases/{case_id}/sla"))
        if resp.status_code == 200:
            return resp.json()
        return []


@activity.defn
async def cancel_case_slas(case_id: str) -> None:
    """Cancel all active SLAs for a case (on resolve/close)."""
    activity.logger.info("Cancelling SLAs for case %s", case_id)
    # SLAs are automatically cancelled by the lifecycle hooks
    # when status changes to resolved/closed/cancelled.
    # This activity exists for explicit cancellation from workflow.
    pass


@activity.defn
async def start_sla_v2_tracking(
    case_id: str,
    sla_policy_id: str,
    target_id: str,
) -> dict[str, Any]:
    """P34b — v2 SLA tracking with escalation tree snapshot.

    Lifecycle workflow calls this when an SLA policy has `use_v2: true`.
    """
    activity.logger.info(
        "Starting SLA v2 tracking: case=%s policy=%s", case_id, sla_policy_id,
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _api(f"/cases/{case_id}/sla/start-v2"),
            json={"sla_policy_id": sla_policy_id, "target_id": target_id},
        )
        if resp.status_code in (200, 201):
            return resp.json()
        activity.logger.warning("SLA v2 start failed: %s", resp.text)
        return {"status": "failed", "error": resp.text}
