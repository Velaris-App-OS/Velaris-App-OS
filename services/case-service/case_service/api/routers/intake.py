"""Intake router — inbound webhook/HTTP trigger → filter evaluation → case creation.

POST /api/v1/intake/webhook/{case_type_id}
  Receives an external payload, evaluates filter_conditions defined on the
  case type, maps payload fields to case fields, creates a CaseInstance,
  and optionally starts a linked HxFusion process.
"""
from __future__ import annotations

import operator
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    IntakeEventModel,
    ProcessDefinitionModel,
    ProcessInstanceModel,
)
from case_service.db.session import get_session

router = APIRouter(prefix="/intake", tags=["intake"])


# ── Filter condition evaluator ────────────────────────────────────────────────

_OPS: dict[str, Any] = {
    "eq":       operator.eq,
    "neq":      operator.ne,
    "gt":       operator.gt,
    "gte":      operator.ge,
    "lt":       operator.lt,
    "lte":      operator.le,
    "contains": lambda a, b: b in str(a),
    "regex":    lambda a, b: bool(re.search(str(b), str(a))),
    "exists":   lambda a, _: a is not None,
}


def _get_path(data: dict, path: str) -> Any:
    """Resolve a dot-notation path from a nested dict. Returns None if missing."""
    parts = path.split(".")
    cur: Any = data
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def evaluate_filters(payload: dict, conditions: dict) -> tuple[bool, list[dict]]:
    """Evaluate filter_conditions against the incoming payload.

    conditions format:
      {
        "logic": "and" | "or",
        "rules": [
          {"field": "event.type", "operator": "eq",  "value": "claim_submitted"},
          {"field": "data.amount", "operator": "gte", "value": 0}
        ]
      }

    Returns (passed: bool, results: list of per-rule outcomes).
    """
    if not conditions or not conditions.get("rules"):
        return True, []   # no filter = accept everything

    logic = conditions.get("logic", "and").lower()
    rules = conditions.get("rules", [])
    results = []

    for rule in rules:
        field   = rule.get("field", "")
        op_name = rule.get("operator", "eq")
        value   = rule.get("value")
        actual  = _get_path(payload, field)
        op_fn   = _OPS.get(op_name)
        try:
            passed = op_fn(actual, value) if op_fn else False
        except Exception:
            passed = False
        results.append({"field": field, "operator": op_name, "expected": value, "actual": actual, "passed": passed})

    passed_list = [r["passed"] for r in results]
    overall = all(passed_list) if logic == "and" else any(passed_list)
    return overall, results


def apply_field_mapping(payload: dict, mapping: dict) -> dict:
    """Map payload dot-paths to case field keys.

    mapping: {"payload.field.path": "case_field_key"}
    Returns a flat dict of case field values.
    """
    result: dict = {}
    for src_path, dest_key in mapping.items():
        val = _get_path(payload, src_path)
        if val is not None:
            result[dest_key] = val
    return result


# ── Intake endpoint ───────────────────────────────────────────────────────────

@router.post("/webhook/{case_type_id}", status_code=201)
async def receive_webhook(
    case_type_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Receive an inbound payload for a case type configured with webhook intake.

    Steps:
    1. Validate the case type exists and has intake_trigger = 'webhook'
    2. Log the inbound event
    3. Evaluate filter_conditions
    4. If passed: create CaseInstance with mapped fields
    5. If process_definition_id set: start HxFusion process bound to the case
    6. Return created case id + process instance id
    """
    now = datetime.now(timezone.utc)

    # Parse body — accept any JSON payload
    try:
        payload: dict = await request.json()
    except Exception:
        payload = {}

    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        raise HTTPException(404, f"Case type {case_type_id} not found")
    if ct.intake_trigger not in ("webhook", "http"):
        raise HTTPException(400, f"Case type '{ct.name}' does not accept webhook intake (trigger={ct.intake_trigger})")

    # Log inbound event
    event = IntakeEventModel(
        case_type_id=case_type_id,
        connector_id=ct.trigger_connector_id,
        source_ip=request.client.host if request.client else None,
        raw_payload=payload,
        status="received",
    )
    session.add(event)
    await session.flush()

    # Evaluate filter conditions
    passed, filter_results = evaluate_filters(payload, ct.filter_conditions or {})
    event.filter_result = {"passed": passed, "rules": filter_results}

    if not passed:
        event.status = "filtered"
        event.processed_at = now
        await session.commit()
        return {
            "accepted": False,
            "reason": "filter_conditions not met",
            "filter_result": filter_results,
            "event_id": str(event.id),
        }

    # Map payload → case fields
    mapped_fields = apply_field_mapping(payload, ct.field_mapping or {})

    # Create the case instance
    case_data = {
        "_intake_source": "webhook",
        "_intake_event_id": str(event.id),
        **mapped_fields,
        # Store full payload for traceability
        "_raw_payload": payload,
    }

    case = CaseInstanceModel(
        case_type_id=case_type_id,
        case_type_version=ct.version,
        tenant_id=ct.tenant_id,
        status="new",
        priority=ct.default_priority,
        data=case_data,
        created_by="intake_webhook",
    )
    session.add(case)
    await session.flush()

    event.created_case_id = case.id
    event.status = "created"

    # Optionally start linked HxFusion process
    process_instance_id = None
    if ct.process_definition_id:
        try:
            from case_service.hxfusion.engine import start_instance as _start_fusion
            from case_service.hxfusion.parser import parse as _parse_bpmn

            defn = await session.get(ProcessDefinitionModel, ct.process_definition_id)
            if defn and defn.status == "active":
                proc_inst = await _start_fusion(
                    definition_id=ct.process_definition_id,
                    case_id=case.id,
                    context={**case_data, "case_type_id": str(case_type_id)},
                    tenant_id=str(ct.tenant_id) if ct.tenant_id else None,
                    stage_id=None,
                    step_id=None,
                    session=session,
                )
                process_instance_id = proc_inst.id
                event.process_instance_id = process_instance_id
        except Exception as _pe:
            # Non-fatal — case is created even if process fails to start
            event.filter_result["process_error"] = str(_pe)

    event.processed_at = now
    await session.commit()

    return {
        "accepted": True,
        "event_id": str(event.id),
        "case_id": str(case.id),
        "case_type": ct.name,
        "process_instance_id": str(process_instance_id) if process_instance_id else None,
        "mapped_fields": list(mapped_fields.keys()),
    }


# ── Intake event log ──────────────────────────────────────────────────────────

@router.get("/events")
async def list_intake_events(
    case_type_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    """List recent intake events with filter/status breakdown."""
    from sqlalchemy import select, desc

    q = select(IntakeEventModel).order_by(desc(IntakeEventModel.received_at)).limit(limit)
    if case_type_id:
        q = q.where(IntakeEventModel.case_type_id == case_type_id)
    if status:
        q = q.where(IntakeEventModel.status == status)

    rows = (await session.execute(q)).scalars().all()
    return {
        "events": [
            {
                "id": str(e.id),
                "case_type_id": str(e.case_type_id) if e.case_type_id else None,
                "status": e.status,
                "created_case_id": str(e.created_case_id) if e.created_case_id else None,
                "process_instance_id": str(e.process_instance_id) if e.process_instance_id else None,
                "filter_result": e.filter_result,
                "source_ip": e.source_ip,
                "received_at": e.received_at.isoformat(),
                "processed_at": e.processed_at.isoformat() if e.processed_at else None,
            }
            for e in rows
        ],
        "total": len(rows),
    }


@router.get("/events/{event_id}")
async def get_intake_event(
    event_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Get full details of an intake event including raw payload."""
    e = await session.get(IntakeEventModel, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    return {
        "id": str(e.id),
        "case_type_id": str(e.case_type_id) if e.case_type_id else None,
        "connector_id": str(e.connector_id) if e.connector_id else None,
        "source_ip": e.source_ip,
        "raw_payload": e.raw_payload,
        "status": e.status,
        "filter_result": e.filter_result,
        "created_case_id": str(e.created_case_id) if e.created_case_id else None,
        "process_instance_id": str(e.process_instance_id) if e.process_instance_id else None,
        "error": e.error,
        "received_at": e.received_at.isoformat(),
        "processed_at": e.processed_at.isoformat() if e.processed_at else None,
    }
