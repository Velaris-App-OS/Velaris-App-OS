"""Form submission router.

Handles submitting a form for a step assignment: validates the form,
merges values into case data, and completes the assignment.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.forms import (
    FormSubmission,
    FormSubmissionResponse,
)
from case_service.db import repository as repo
from case_service.auth.dependencies import get_current_user
from case_service.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/form-submissions", tags=["form-submissions"], dependencies=[Depends(get_current_user)])


def _validate_form_values(
    definition: dict[str, Any], values: dict[str, Any]
) -> list[str]:
    """Validate submitted values against form definition. Returns errors."""
    errors: list[str] = []
    sections = definition.get("sections", [])

    # Build a map of all fields
    field_map: dict[str, dict[str, Any]] = {}
    for section in sections:
        for field in section.get("fields", []):
            key = field.get("field_key") or field.get("id", "")
            field_map[key] = field

    # Check required fields
    for key, field_def in field_map.items():
        if field_def.get("required") and key not in values:
            errors.append(f"Required field '{field_def.get('label', key)}' is missing")
        val = values.get(key)
        if val is None:
            continue

        ftype = field_def.get("type", "text")
        validation = field_def.get("validation", {})

        if ftype in ("text", "textarea", "email", "phone") and isinstance(val, str):
            min_len = validation.get("min_length")
            max_len = validation.get("max_length")
            if min_len and len(val) < min_len:
                errors.append(f"'{field_def.get('label', key)}' must be at least {min_len} characters")
            if max_len and len(val) > max_len:
                errors.append(f"'{field_def.get('label', key)}' must be at most {max_len} characters")
            if ftype == "email" and "@" not in str(val):
                errors.append(f"'{field_def.get('label', key)}' must be a valid email")

        if ftype == "number" and val is not None:
            try:
                num = float(val)
                min_val = validation.get("min")
                max_val = validation.get("max")
                if min_val is not None and num < min_val:
                    errors.append(f"'{field_def.get('label', key)}' must be >= {min_val}")
                if max_val is not None and num > max_val:
                    errors.append(f"'{field_def.get('label', key)}' must be <= {max_val}")
            except (ValueError, TypeError):
                errors.append(f"'{field_def.get('label', key)}' must be a number")

        if ftype == "dropdown":
            options = [o.get("value") if isinstance(o, dict) else o for o in field_def.get("options", [])]
            if options and val not in options:
                errors.append(f"'{field_def.get('label', key)}' must be one of {options}")

    return errors


@router.post(
    "/{assignment_id}",
    response_model=FormSubmissionResponse,
)
async def submit_form(
    assignment_id: uuid.UUID,
    body: FormSubmission,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Submit a form for a step assignment.

    1. Validates the assignment exists and is active
    2. Loads the form definition and validates submitted values
    3. Merges form values into case data under form namespace
    4. Completes the assignment
    5. Signals the Temporal workflow
    """
    # 1. Load assignment
    assignment = await repo.get_assignment(session, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if assignment.status != "active":
        raise HTTPException(status_code=409, detail="Assignment is not active")

    # 2. Load and validate form
    form = await repo.get_form(session, body.form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")

    errors = _validate_form_values(form.definition_json, body.values)
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    # 3. Merge form data into case
    case = await repo.get_case_instance(session, assignment.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    merged_data = dict(case.data or {})
    form_namespace = f"form_{str(body.form_id).replace('-', '_')[:8]}"
    merged_data[form_namespace] = body.values
    # Also put values at top level for convenience
    merged_data.update(body.values)

    await repo.update_case_instance(
        session, assignment.case_id, values={"data": merged_data}
    )

    # 4. Complete assignment
    now = datetime.now(timezone.utc)
    await repo.update_assignment(
        session,
        assignment_id,
        values={"status": "completed", "completed_at": now},
    )

    # 5. Audit
    await repo.append_audit_entry(
        session,
        data={
            "case_id": assignment.case_id,
            "action": "form_submitted",
            "actor_id": body.completed_by,
            "details": {
                "assignment_id": str(assignment_id),
                "form_id": str(body.form_id),
                "step_id": assignment.step_id,
                "field_count": len(body.values),
            },
        },
    )

    # 6. Signal Temporal
    temporal_client = getattr(request.app.state, "temporal_client", None)
    if temporal_client is not None:
        if case.process_instance_id:
            try:
                handle = temporal_client.get_workflow_handle(
                    case.process_instance_id
                )
                await handle.signal(
                    "step_completed",
                    {
                        "step_id": assignment.step_id,
                        "completed_by": body.completed_by or "system",
                        "form_data": body.values,
                    },
                )
                logger.info(
                    "Signaled workflow %s: form submitted for step %s",
                    case.process_instance_id,
                    assignment.step_id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to signal workflow %s: %s",
                    case.process_instance_id,
                    e,
                )

    return FormSubmissionResponse(
        assignment_id=assignment_id,
        case_id=assignment.case_id,
        form_id=body.form_id,
        values=body.values,
        status="completed",
    )


@router.get("/{assignment_id}/form")
async def get_assignment_form(
    assignment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Get the form definition linked to an assignment's step.

    Looks up the step's form_id from the case type definition.
    """
    assignment = await repo.get_assignment(session, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")

    case = await repo.get_case_instance(session, assignment.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    case_type = await repo.get_case_type(session, case.case_type_id)
    if case_type is None:
        raise HTTPException(status_code=404, detail="Case type not found")

    # Find form_id from step definition
    definition = case_type.definition_json or {}
    form_id = None
    for stage in definition.get("stages", []):
        for step in stage.get("steps", []):
            if step.get("id") == assignment.step_id:
                form_id = step.get("form_id")
                break
        if form_id:
            break

    if not form_id:
        return {"has_form": False, "form": None}

    # Try as UUID first, then as string lookup
    try:
        form_uuid = uuid.UUID(form_id)
        form = await repo.get_form(session, form_uuid)
    except (ValueError, AttributeError):
        form = None

    if form is None:
        return {"has_form": False, "form": None, "form_id_ref": form_id}

    return {
        "has_form": True,
        "form": {
            "id": str(form.id),
            "name": form.name,
            "version": form.version,
            "definition_json": form.definition_json,
        },
    }
