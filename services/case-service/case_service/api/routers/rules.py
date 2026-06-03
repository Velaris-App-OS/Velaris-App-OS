"""Rules API router.

CRUD for rule definitions and evaluation endpoints.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.rules import (
    DataValidateRequest,
    DataValidateResponse,
    RuleBatchEvaluateRequest,
    RuleCreate,
    RuleEvaluateRequest,
    RuleEvaluateResponse,
    RuleListResponse,
    RuleResponse,
    RuleUpdate,
)
from case_service.core.rules_evaluator import (
    evaluate_rule as eval_rule,
    evaluate_rules as eval_rules,
    validate_data as validate_data_fn,
)
from case_service.db import repository as repo
from case_service.auth.dependencies import get_current_user
from case_service.db.session import get_session

router = APIRouter(prefix="/rules", tags=["rules"], dependencies=[Depends(get_current_user)])


# ─── CRUD ─────────────────────────────────────────────────────────


@router.post("", response_model=RuleResponse, status_code=201)
async def create_rule(
    body: RuleCreate,
    session: AsyncSession = Depends(get_session),
):
    rule = await repo.create_rule(
        session,
        data={
            "name": body.name,
            "version": body.version,
            "rule_type": body.rule_type,
            "scope": body.scope,
            "scope_target_id": body.scope_target_id,
            "definition_json": body.definition_json,
            "enabled": body.enabled,
            "priority": body.priority,
        },
    )
    return rule


@router.get("", response_model=RuleListResponse)
async def list_rules(
    rule_type: str | None = None,
    scope: str | None = None,
    enabled: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    items, total = await repo.list_rules(
        session,
        rule_type=rule_type,
        scope=scope,
        enabled=enabled,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return RuleListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.get("/{rule_id}", response_model=RuleResponse)
async def get_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


@router.patch("/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: uuid.UUID,
    body: RuleUpdate,
    session: AsyncSession = Depends(get_session),
):
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    values = {}
    if body.definition_json is not None:
        values["definition_json"] = body.definition_json
    if body.enabled is not None:
        values["enabled"] = body.enabled
    if body.priority is not None:
        values["priority"] = body.priority

    if values:
        await repo.update_rule(session, rule_id, values=values)

    return await repo.get_rule(session, rule_id)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    deleted = await repo.delete_rule(session, rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Rule not found")


# ─── Evaluation ───────────────────────────────────────────────────


@router.post(
    "/{rule_id}/evaluate", response_model=RuleEvaluateResponse
)
async def evaluate_rule(
    rule_id: uuid.UUID,
    body: RuleEvaluateRequest,
    session: AsyncSession = Depends(get_session),
):
    """Evaluate a single rule against provided context."""
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    if not rule.enabled:
        raise HTTPException(status_code=409, detail="Rule is disabled")

    # Merge stored definition with rule metadata for evaluation
    rule_dict = {
        "id": str(rule.id),
        "name": rule.name,
        "rule_type": rule.rule_type,
        "priority": rule.priority,
        **rule.definition_json,
    }
    result = eval_rule(rule_dict, body.context)
    return RuleEvaluateResponse(rule_id=str(rule.id), result=result)


@router.post("/evaluate/batch")
async def evaluate_rules_batch(
    body: RuleBatchEvaluateRequest,
    session: AsyncSession = Depends(get_session),
):
    """Evaluate multiple rules against the same context."""
    rule_dicts = []
    for rid in body.rule_ids:
        rule = await repo.get_rule(session, rid)
        if rule is None:
            raise HTTPException(
                status_code=404, detail=f"Rule {rid} not found"
            )
        if rule.enabled:
            rule_dicts.append({
                "id": str(rule.id),
                "name": rule.name,
                "rule_type": rule.rule_type,
                "priority": rule.priority,
                **rule.definition_json,
            })

    results = eval_rules(rule_dicts, body.context)
    return {"results": results, "evaluated_count": len(results)}


@router.post("/validate-data", response_model=DataValidateResponse)
async def validate_data(
    body: DataValidateRequest,
    session: AsyncSession = Depends(get_session),
):
    """Validate data against a data model's field definitions."""
    dm = await repo.get_data_model(session, body.data_model_id)
    if dm is None:
        raise HTTPException(
            status_code=404, detail="Data model not found"
        )

    result = validate_data_fn(dm.definition_json, body.data)
    return DataValidateResponse(**result)
