"""Pydantic schemas for the case-service REST API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# ─── Case Types ───────────────────────────────────────────────────────


class CaseTypeCreate(BaseModel):
    """Request body: deploy a new case type."""

    name: str
    version: str
    tenant_id: UUID | None = None
    lifecycle_process_id: str | None = None
    data_model_id: UUID | None = None
    security_profile_id: UUID | None = None
    default_priority: str = "medium"
    definition_json: dict[str, Any]
    icon: str | None = None
    color: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    # Intake trigger
    intake_trigger: str = "manual"
    trigger_connector_id: UUID | None = None
    filter_conditions: dict[str, Any] = Field(default_factory=dict)
    field_mapping: dict[str, Any] = Field(default_factory=dict)
    process_definition_id: UUID | None = None


class CaseTypeResponse(BaseModel):
    """Response: case type details."""

    id: UUID
    tenant_id: UUID | None = None
    name: str
    version: str
    lifecycle_process_id: str | None
    data_model_id: UUID | None
    default_priority: str
    description: str
    tags: list[str]
    icon: str | None
    color: str | None
    definition_json: dict = {}
    # Intake trigger
    intake_trigger: str = "manual"
    trigger_connector_id: UUID | None = None
    filter_conditions: dict = {}
    field_mapping: dict = {}
    process_definition_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CaseTypeListResponse(BaseModel):
    items: list[CaseTypeResponse]
    total: int
    page: int
    page_size: int


# ─── Case Instances ───────────────────────────────────────────────────


class CaseCreate(BaseModel):
    """Request body: create a new case."""

    case_type_id: UUID
    data: dict[str, Any] = Field(default_factory=dict)
    priority: str | None = None
    parent_case_id: UUID | None = None
    created_by: str | None = None


class CaseDataUpdate(BaseModel):
    """Request body: update case data fields."""

    data: dict[str, Any]
    updated_by: str | None = None


class CaseStatusChange(BaseModel):
    """Request body: change case status."""

    status: str
    reason: str | None = None
    actor_id: str | None = None


class CasePriorityChange(BaseModel):
    """Request body: change case priority."""

    priority: str
    actor_id: str | None = None


class CaseStageTransition(BaseModel):
    """Request body: move case to a stage."""

    target_stage_id: str
    actor_id: str | None = None


class CaseResolve(BaseModel):
    """Request body: resolve a case."""

    resolution: dict[str, Any] | None = None
    actor_id: str | None = None


class CaseAction(BaseModel):
    """Generic action body (close, reopen, cancel)."""

    reason: str | None = None
    actor_id: str | None = None


# P38 — Step completion schemas

class StepCompleteBody(BaseModel):
    """Request body: complete or reject a step."""
    stage_id: str
    step_type: str = "user_task"
    status: str = "completed"          # completed | rejected
    data: dict[str, Any] = {}          # form values, approval reason, doc ref, etc.
    actor_id: str | None = None


class StepCompletionResponse(BaseModel):
    id: str
    case_id: str
    stage_id: str
    step_id: str
    step_type: str
    status: str
    data: dict[str, Any]
    completed_by: str | None
    completed_at: str
    auto_advanced: bool = False        # True if stage was auto-advanced after this completion


class CaseResponse(BaseModel):
    """Response: case instance details."""

    id: UUID
    case_number: str | None = None
    case_type_id: UUID
    case_type_version: str
    process_instance_id: str | None
    status: str
    priority: str
    urgency_score: float
    current_stage_id: str | None
    parent_case_id: UUID | None
    data: dict[str, Any]
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None

    model_config = {"from_attributes": True}


class CaseListResponse(BaseModel):
    items: list[CaseResponse]
    total: int
    page: int
    page_size: int


# ─── Assignments ──────────────────────────────────────────────────────


class AssignmentResponse(BaseModel):
    id: UUID
    case_id: UUID
    step_id: str
    assignee_type: str
    assignee_id: str
    status: str
    assigned_at: datetime
    due_at: datetime | None
    claimed_at: datetime | None
    completed_at: datetime | None
    assigned_by: str | None

    model_config = {"from_attributes": True}


class AssignmentClaim(BaseModel):
    user_id: str


class AssignmentReassign(BaseModel):
    new_assignee_id: str
    reason: str | None = None


class AssignmentComplete(BaseModel):
    result: dict[str, Any] | None = None
    completed_by: str | None = None


# ─── Relationships ────────────────────────────────────────────────────


class RelationshipCreate(BaseModel):
    target_case_id: UUID
    relationship_type: str
    propagate_status: bool = False
    propagate_priority: bool = False
    required: bool = False


class RelationshipResponse(BaseModel):
    id: UUID
    source_case_id: UUID
    target_case_id: UUID
    relationship_type: str
    propagate_status: bool
    propagate_priority: bool
    required: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Work Queues ──────────────────────────────────────────────────────


class WorkQueueCreate(BaseModel):
    name: str
    description: str = ""
    tenant_id: UUID | None = None
    filter_criteria: dict[str, Any] = Field(default_factory=dict)
    sort_fields: list[str] = Field(default_factory=lambda: ["urgency"])
    sort_ascending: bool = True
    visible_to_roles: list[str] = Field(default_factory=list)
    auto_assignment: bool = False
    urgency_formula: str | None = None
    max_items: int | None = None


class WorkQueueResponse(BaseModel):
    id: UUID
    tenant_id: UUID | None
    name: str
    description: str
    filter_criteria: dict[str, Any]
    sort_fields: list[str]
    sort_ascending: bool
    visible_to_roles: list[str]
    auto_assignment: bool
    urgency_formula: str | None
    max_items: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class QueueStatsResponse(BaseModel):
    queue_id: UUID
    total_items: int
    active_items: int
    avg_wait_seconds: float | None
    sla_on_track: int
    sla_at_risk: int
    sla_breached: int


# ─── Audit ────────────────────────────────────────────────────────────


class AuditEntryResponse(BaseModel):
    id: UUID
    case_id: UUID
    action: str
    actor_id: str | None
    actor_type: str
    timestamp: datetime
    details: dict[str, Any]
    previous_value: dict[str, Any] | None
    new_value: dict[str, Any] | None

    model_config = {"from_attributes": True}


# ─── SLA ──────────────────────────────────────────────────────────────


class SLAStatusResponse(BaseModel):
    id: UUID
    case_id: UUID
    sla_policy_id: str
    target_id: str
    status: str
    started_at: datetime
    goal_at: datetime
    deadline_at: datetime
    paused_duration_seconds: int
    breached_at: datetime | None

    model_config = {"from_attributes": True}
