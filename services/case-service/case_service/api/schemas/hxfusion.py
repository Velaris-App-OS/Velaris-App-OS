"""Pydantic schemas for P47 HxFusion."""
from __future__ import annotations
import uuid
from datetime import datetime
from pydantic import BaseModel, Field


# ── Process Definitions ───────────────────────────────────────────────────────

class ProcessDefinitionCreate(BaseModel):
    name: str
    bpmn_xml: str
    description: str | None = None
    case_type_id: str | None = None
    created_by: str | None = None
    tenant_id: str | None = None


class ProcessDefinitionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    bpmn_xml: str | None = None
    case_type_id: str | None = None
    status: str | None = None


class ProcessDefinitionOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    description: str | None
    case_type_id: str | None
    status: str
    created_by: str | None
    tenant_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Process Instances ─────────────────────────────────────────────────────────

class StartProcessRequest(BaseModel):
    definition_id: uuid.UUID
    case_id: uuid.UUID | None = None
    context: dict = Field(default_factory=dict)
    tenant_id: str | None = None
    stage_id: str | None = None
    step_id: str | None = None


class ProcessInstanceOut(BaseModel):
    id: uuid.UUID
    definition_id: uuid.UUID
    case_id: uuid.UUID | None
    status: str
    current_node: str | None
    context: dict
    error_node: str | None
    error_message: str | None
    started_at: datetime
    ended_at: datetime | None
    tenant_id: str | None

    model_config = {"from_attributes": True}


class ResumeRequest(BaseModel):
    resolution: dict = Field(default_factory=dict)
    resumed_by: str | None = None


# ── Bindings ──────────────────────────────────────────────────────────────────

class ProcessCaseBindingOut(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID
    instance_id: uuid.UUID
    binding_type: str
    direction: str
    status: str
    stage_id: str | None
    step_id: str | None
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


# ── Task Log ──────────────────────────────────────────────────────────────────

class ProcessTaskLogOut(BaseModel):
    id: uuid.UUID
    instance_id: uuid.UUID
    node_id: str
    node_name: str | None
    node_type: str
    status: str
    input_context: dict
    result: dict | None
    error: str | None
    started_at: datetime
    ended_at: datetime | None

    model_config = {"from_attributes": True}


# ── AI Director ───────────────────────────────────────────────────────────────

class AIDirectorRequest(BaseModel):
    case_id: uuid.UUID
    stage_id: str
    case_type_id: str | None = None
    context: dict = Field(default_factory=dict)


class AIDirectorResponse(BaseModel):
    can_automate: bool
    confidence: float
    suggestion: str
    recommended_definition_id: uuid.UUID | None
    reasoning: str
