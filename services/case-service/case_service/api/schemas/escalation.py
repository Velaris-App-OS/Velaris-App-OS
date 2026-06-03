"""Escalation tree + SLA v2 schemas."""
from __future__ import annotations
import uuid
from typing import Any, Optional
from pydantic import BaseModel, Field


class EscalationTrigger(BaseModel):
    type: str = Field(..., pattern=r"^(goal_pct|deadline_pct|fixed_duration|at_breach)$")
    value: Any = None


class EscalationAction(BaseModel):
    type: str = Field(..., pattern=r"^(notify|reassign|priority|status)$")
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    message: Optional[str] = None
    set: Optional[str] = None


class EscalationLevel(BaseModel):
    level: int = Field(..., ge=1, le=99)
    name: str
    trigger: EscalationTrigger
    actions: list[EscalationAction] = []


class EscalationTreeBody(BaseModel):
    levels: list[EscalationLevel] = []


class EscalationTreeCreate(BaseModel):
    name: str
    description: str = ""
    scope: str = Field("global", pattern=r"^(global|case_type)$")
    case_type_id: Optional[uuid.UUID] = None
    tenant_id: Optional[str] = None
    tree_json: EscalationTreeBody
    is_active: bool = True


class EscalationTreeUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    tree_json: Optional[EscalationTreeBody] = None
    is_active: Optional[bool] = None


class EscalationTreeResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    scope: str
    case_type_id: Optional[uuid.UUID]
    tenant_id: Optional[str]
    tree_json: dict
    is_active: bool
    created_by: Optional[str]
    created_at: str
    updated_at: str


class SLAPauseRequest(BaseModel):
    reason: str = Field(..., min_length=2, max_length=255)
    actor_id: Optional[str] = None


class SLAResumeRequest(BaseModel):
    actor_id: Optional[str] = None


class EscalationPreviewRequest(BaseModel):
    tree_json: EscalationTreeBody
    goal_duration: str = "PT4H"
    deadline_duration: str = "PT24H"
    started_at: Optional[str] = None  # ISO; defaults to now
