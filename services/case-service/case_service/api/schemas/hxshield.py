"""Pydantic schemas for P59 HxShield."""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


# ── Security Rules ────────────────────────────────────────────────────────────

class SecurityRuleCreate(BaseModel):
    name: str
    pattern_type: str
    description: str | None = None
    threshold: int = 10
    window_seconds: int = 600
    action: str = "flag"
    severity: str = "medium"
    enabled: bool = True
    tenant_id: str | None = None
    created_by: str | None = None


class SecurityRuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    threshold: int | None = None
    window_seconds: int | None = None
    action: str | None = None
    severity: str | None = None
    enabled: bool | None = None


class SecurityRuleOut(BaseModel):
    id: uuid.UUID
    name: str
    pattern_type: str
    description: str | None
    threshold: int
    window_seconds: int
    action: str
    severity: str
    enabled: bool
    tenant_id: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Security Incidents ────────────────────────────────────────────────────────

class SecurityIncidentOut(BaseModel):
    id: uuid.UUID
    rule_id: uuid.UUID | None
    pattern_type: str
    severity: str
    status: str
    actor_id: str | None
    tenant_id: str | None
    case_type_id: str | None
    context: dict
    explanation: str | None
    detected_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None

    model_config = {"from_attributes": True}


class IncidentResolve(BaseModel):
    resolved_by: str


# ── Shield Events ─────────────────────────────────────────────────────────────

class ShieldEventOut(BaseModel):
    id: uuid.UUID
    event_type: str
    actor_id: str | None
    tenant_id: str | None
    case_type_id: str | None
    payload_hash: str | None
    score: float
    patterns_matched: list
    raw_context: dict
    recorded_at: datetime

    model_config = {"from_attributes": True}


# ── Score Request ─────────────────────────────────────────────────────────────

class ScoreRequest(BaseModel):
    event_type: str
    actor_id: str | None = None
    tenant_id: str | None = None
    case_type_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ScoreResponse(BaseModel):
    score: float
    patterns_matched: list[str]
    action: str
    incident_id: uuid.UUID | None
    explanation: str | None
