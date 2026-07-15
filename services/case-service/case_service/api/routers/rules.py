"""Rules API router.

CRUD for rule definitions and evaluation endpoints.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
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
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.hxguard import service as hxguard

router = APIRouter(prefix="/rules", tags=["rules"], dependencies=[Depends(get_current_user)])

_EXPRESSION_RULE_TYPES = ("expression", "declare_expression")


def _validate_expression_rule(rule_type: str, definition_json: dict) -> None:
    """Hard-reject non-CONFORMING expression rules at write time.

    HxSandbox #17 Phase 1: new expression rules must parse within the strict
    safe grammar, so attacker-supplied input never reaches the hardened
    ``eval`` fallback (which exists only to keep pre-existing rules running
    during the deprecation window). Raises HTTP 400 otherwise.
    """
    if rule_type not in _EXPRESSION_RULE_TYPES:
        return
    from case_service.core.safe_expression import (
        Classification, classify_expression,
    )
    expression = (definition_json or {}).get("expression", "")
    classification, reason = classify_expression(expression)
    if classification is not Classification.CONFORMING:
        raise HTTPException(
            status_code=400,
            detail=f"Expression rejected by HxSandbox: {reason or classification.value}",
        )


# ─── CRUD ─────────────────────────────────────────────────────────


@router.post("", response_model=RuleResponse, status_code=201)
async def create_rule(
    body: RuleCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("rules.write")),
):
    _validate_expression_rule(body.rule_type, body.definition_json)
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
    # #27 Part B: a rule change can affect AI scenarios → flag them stale (manual
    # regen). scope_target_id is the case type for case-type-scoped rules; None
    # (global) flags all generated suites.
    from case_service.testsuite import regen
    background_tasks.add_task(regen.bg_scenario_source_changed, body.scope_target_id)
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


@router.get("/lint")
async def lint_expression_rules(
    scope: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Classify every stored expression rule (roadmap #28, pulled forward).

    HxSandbox #17 Phase 1: reports, per rule, whether it is CONFORMING (runs
    on the safe evaluator), NEEDS_MIGRATION (still runs on the hardened
    fallback during the deprecation window), or REJECTED (escape attempt /
    over-limit — returns None at runtime). No rule is migrated automatically.
    """
    from case_service.core.safe_expression import (
        Classification, classify_expression,
    )

    findings: list[dict] = []
    summary = {c.value: 0 for c in Classification}
    for rule_type in _EXPRESSION_RULE_TYPES:
        items, _ = await repo.list_rules(
            session, rule_type=rule_type, scope=scope, offset=0, limit=10_000,
        )
        for rule in items:
            expression = (rule.definition_json or {}).get("expression", "")
            classification, reason = classify_expression(expression)
            summary[classification.value] += 1
            findings.append({
                "rule_id": str(rule.id),
                "name": rule.name,
                "rule_type": rule.rule_type,
                "scope": rule.scope,
                "scope_target_id": rule.scope_target_id,
                "enabled": rule.enabled,
                "classification": classification.value,
                "reason": reason,
                "expression": expression,
            })

    return {
        "summary": summary,
        "total": len(findings),
        "findings": findings,
    }


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
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("rules.write")),
):
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    values = {}
    if body.definition_json is not None:
        _validate_expression_rule(rule.rule_type, body.definition_json)
        values["definition_json"] = body.definition_json
    if body.enabled is not None:
        values["enabled"] = body.enabled
    if body.priority is not None:
        values["priority"] = body.priority

    if values:
        await repo.update_rule(session, rule_id, values=values)
        # #27 Part B: rule changed → flag affected AI scenarios stale (manual regen).
        from case_service.testsuite import regen
        background_tasks.add_task(regen.bg_scenario_source_changed, rule.scope_target_id)

    return await repo.get_rule(session, rule_id)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("rules.write")),
):
    rule = await repo.get_rule(session, rule_id)         # capture target before delete
    deleted = await repo.delete_rule(session, rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Rule not found")
    # #27 Part B: rule removed → flag affected AI scenarios stale (manual regen).
    from case_service.testsuite import regen
    background_tasks.add_task(regen.bg_scenario_source_changed,
                              rule.scope_target_id if rule else None)


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
