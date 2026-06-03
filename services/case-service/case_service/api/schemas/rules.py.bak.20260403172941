"""Pydantic schemas for the rules API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class RuleCreate(BaseModel):
    name: str
    version: str
    rule_type: str
    scope: str = "global"
    scope_target_id: str | None = None
    definition_json: dict[str, Any]
    enabled: bool = True
    priority: int = 0


class RuleUpdate(BaseModel):
    definition_json: dict[str, Any] | None = None
    enabled: bool | None = None
    priority: int | None = None


class RuleResponse(BaseModel):
    id: UUID
    name: str
    version: str
    rule_type: str
    scope: str
    scope_target_id: str | None
    definition_json: dict[str, Any]
    enabled: bool
    priority: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RuleListResponse(BaseModel):
    items: list[RuleResponse]
    total: int
    page: int
    page_size: int


class RuleEvaluateRequest(BaseModel):
    """Request body for evaluating a rule against context."""
    context: dict[str, Any]


class RuleEvaluateResponse(BaseModel):
    rule_id: str
    result: dict[str, Any]


class RuleBatchEvaluateRequest(BaseModel):
    """Evaluate multiple rules against the same context."""
    rule_ids: list[UUID]
    context: dict[str, Any]


class DataValidateRequest(BaseModel):
    """Validate data against a data model's rules."""
    data_model_id: UUID
    data: dict[str, Any]


class DataValidateResponse(BaseModel):
    valid: bool
    errors: list[dict[str, Any]]
    field_count: int
    validated_count: int
