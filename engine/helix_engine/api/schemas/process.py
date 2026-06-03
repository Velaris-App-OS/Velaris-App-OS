"""
Process API Schemas
====================

Pydantic models for the process management API.

These define the HTTP request/response shapes.  They are separate from
the IR models — the IR is the internal representation, these are the
external contract.

Why separate?
  - IR models may change without breaking the API.
  - API schemas handle validation, serialisation, and documentation.
  - Clients never see internal compiler details.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════
#  Enums
# ═══════════════════════════════════════════════════════════════════════

class ProcessStatus(str, Enum):
    """Lifecycle status of a deployed process definition."""
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEPRECATED = "deprecated"


class InstanceStatus(str, Enum):
    """Execution status of a process instance."""
    RUNNING = "running"
    WAITING_USER_TASK = "waiting_user_task"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"


# ═══════════════════════════════════════════════════════════════════════
#  Deploy
# ═══════════════════════════════════════════════════════════════════════

class DeployRequest(BaseModel):
    """
    POST /processes/deploy

    Deploy a BPMN 2.0 process definition to the engine.
    The XML is compiled, validated, and stored.
    """
    bpmn_xml: str = Field(
        ...,
        description="Raw BPMN 2.0 XML content",
        min_length=1,
    )
    name: str | None = Field(
        None,
        description="Optional display name (overrides the name in XML)",
    )
    tags: dict[str, str] = Field(
        default_factory=dict,
        description="Key-value tags for filtering and organisation",
    )


class DeployResponse(BaseModel):
    """Response after successful deployment."""
    process_id: str = Field(..., description="BPMN process id from the XML")
    version: int = Field(..., description="Auto-incremented version number")
    name: str | None = Field(None, description="Process display name")
    status: ProcessStatus = Field(ProcessStatus.ACTIVE)
    element_count: int = Field(..., description="Number of BPMN elements")
    flow_count: int = Field(..., description="Number of sequence flows")
    warnings: list[str] = Field(default_factory=list, description="Compilation warnings")
    deployed_at: datetime
    bpmn_xml: str | None = Field(None, description="Original BPMN XML (included on detail endpoints)")


# ═══════════════════════════════════════════════════════════════════════
#  Start instance
# ═══════════════════════════════════════════════════════════════════════

class StartRequest(BaseModel):
    """
    POST /processes/{process_id}/start

    Start a new instance of a deployed process.
    """
    variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Initial process variables (input data)",
    )
    business_key: str | None = Field(
        None,
        description="Optional business key for correlation (e.g. order_id)",
    )


class StartResponse(BaseModel):
    """Response after starting a process instance."""
    instance_id: str = Field(..., description="Unique instance identifier")
    process_id: str = Field(..., description="The deployed process definition id")
    version: int = Field(..., description="Version of the process definition used")
    status: InstanceStatus = Field(InstanceStatus.RUNNING)
    business_key: str | None = None
    started_at: datetime


# ═══════════════════════════════════════════════════════════════════════
#  Instance status
# ═══════════════════════════════════════════════════════════════════════

class PendingUserTask(BaseModel):
    """Details of a user task waiting for form submission."""
    task_id: str
    task_name: str | None = None
    form_key: str | None = None


class InstanceStatusResponse(BaseModel):
    """
    GET /processes/{process_id}/instances/{instance_id}

    Current status of a running or completed process instance.
    """
    instance_id: str
    process_id: str
    version: int
    status: InstanceStatus
    business_key: str | None = None
    variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Current process variables",
    )
    visited_elements: list[str] = Field(
        default_factory=list,
        description="Ordered list of element ids that have been executed",
    )
    pending_user_task: PendingUserTask | None = Field(
        None,
        description="User task currently waiting for form submission",
    )
    error: str | None = Field(None, description="Error message if failed")
    started_at: datetime
    completed_at: datetime | None = None


# ═══════════════════════════════════════════════════════════════════════
#  List / query
# ═══════════════════════════════════════════════════════════════════════

class ProcessSummary(BaseModel):
    """Summary of a deployed process definition (for list endpoints)."""
    process_id: str
    version: int
    name: str | None = None
    status: ProcessStatus
    element_count: int
    flow_count: int
    tags: dict[str, str] = Field(default_factory=dict)
    deployed_at: datetime
    bpmn_xml: str | None = Field(None, description="Original BPMN XML (included on detail endpoints)")


class InstanceSummary(BaseModel):
    """Summary of a process instance (for list endpoints)."""
    instance_id: str
    process_id: str
    version: int
    status: InstanceStatus
    business_key: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class ProcessListResponse(BaseModel):
    """GET /processes — list all deployed process definitions."""
    processes: list[ProcessSummary]
    total: int


class InstanceListResponse(BaseModel):
    """GET /processes/{process_id}/instances — list instances of a process."""
    instances: list[InstanceSummary]
    total: int


# ═══════════════════════════════════════════════════════════════════════
#  Error response
# ═══════════════════════════════════════════════════════════════════════

class CompleteTaskRequest(BaseModel):
    """POST /processes/{pid}/instances/{iid}/complete-task — submit a user task form."""
    task_id: str = Field(..., description="The BPMN user task element id")
    variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Form submission data to merge into process variables",
    )


class ScheduleRequest(BaseModel):
    """POST /processes/{pid}/schedules — create a recurring schedule."""
    cron: str = Field(..., description="Cron expression, e.g. '0 9 * * 1-5'")
    variables: dict[str, Any] = Field(default_factory=dict, description="Variables to pass on each run")
    business_key_prefix: str | None = Field(None, description="Prefix for auto-generated business keys")
    description: str | None = None


class ScheduleResponse(BaseModel):
    """Response after creating a schedule."""
    schedule_id: str
    process_id: str
    cron: str
    status: str = "active"


class ErrorResponse(BaseModel):
    """Standard error response body."""
    error: str = Field(..., description="Error type")
    detail: str = Field(..., description="Human-readable error message")
    element_id: str | None = Field(None, description="BPMN element that caused the error")
