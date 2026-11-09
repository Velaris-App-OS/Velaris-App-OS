"""Form submission router.

Handles submitting a form for a step assignment: validates the form,
merges values into case data, and completes the assignment.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.forms import (
    FormSubmission,
    FormSubmissionResponse,
)
from velaris_shared.db import repository as repo
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.hxguard.step_submit import acts_as, require_step_submit
from velaris_shared.db.session import get_session
from velaris_shared.forms import field_key_of, form_keys, validate as validate_form
from velaris_shared.forms.pickers import picker_lookups
from velaris_shared.forms.security import caller_roles, secure_definition
from velaris_shared.forms.translate import for_caller
from velaris_shared.forms.versions import form_for_case

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/form-submissions", tags=["form-submissions"], dependencies=[Depends(get_current_user)])


def _validate_form_values(
    definition: dict[str, Any], values: dict[str, Any]
) -> list[str]:
    """Validate submitted values against form definition. Returns errors.

    The rules are `velaris_shared.forms.validate` (2026-10-28), shared with
    step completion and mirrored by Studio.
    """
    labels = {field_key_of(f): f.get("label") or field_key_of(f)
              for sec in (definition or {}).get("sections", []) for f in sec.get("fields", [])}
    _, errors = validate_form(definition, values)
    return [f"'{labels.get(e.field_key, e.field_key)}': {e.message}" for e in errors]


async def case_context(session: AsyncSession, user: Any, case_id: Any) -> dict:
    """The case's own values, as Studio pre-fills them (`GET /variables/cases/{id}`).

    A read-only field holds what the case holds, so a formula or a rule reads
    that — never a value the caller sent for a field they cannot change
    (Form Builder v2, 2026-10-28). When it cannot be read: `{}` — a read-only
    field is then its default, still never the caller's value (fail closed).
    """
    try:
        from case_service.api.routers.case_variables import _operator_ctx
        from case_service.case_vars import get_all as vars_get_all
        async with session.begin_nested():          # a failed read must not abort the submit
            return await vars_get_all(session, _operator_ctx(user), case_id)
    except Exception:                               # noqa: BLE001 — never blocks a submit
        logger.warning("case values unavailable for form check on %s", case_id, exc_info=True)
        return {}


async def form_references(session: AsyncSession, definition: Any, tenant_id: Any) -> dict:
    """The reference rows the form's fields choose from (Form Builder v2 step 3),
    for the server to check every chosen value against. When they cannot be
    read: every table empty — nothing chosen from one is accepted (fail closed)."""
    from velaris_shared.forms.reference import load_rows, table_ids
    if not table_ids(definition):
        return {}
    try:
        async with session.begin_nested():
            return await load_rows(session, definition, tenant_id)
    except Exception:                               # noqa: BLE001
        logger.warning("reference rows unavailable for form check", exc_info=True)
        return {t: [] for t in table_ids(definition)}


async def reference_tenant(session: AsyncSession, user: Any, case_row: Any) -> Any:
    """Whose reference tables a form reads on submit: the case's tenant, else —
    a case with no tenant — the caller's (the same the options route uses)."""
    tid = getattr(case_row, "tenant_id", None)
    if tid is not None:
        return tid
    from velaris_shared.tenancy.identity import caller_tenant_uuid
    return await caller_tenant_uuid(session, user)


async def _step_form_id(session: AsyncSession, case: Any, step_id: str | None) -> str | None:
    """The form the case type's step names, if any."""
    if case is None or not step_id:
        return None
    from velaris_shared.db.models import CaseTypeModel
    ct = await session.get(CaseTypeModel, case.case_type_id)
    for sg in ((ct.definition_json or {}) if ct else {}).get("stages", []) or []:
        for st in sg.get("steps", []) or []:
            if st.get("id") == step_id:
                return st.get("form_id")
    return None


def _values_fingerprint(values: dict | None) -> str:
    """Stable hash of submitted form values — the replay signal without the data.

    Sorted keys so the same answers hash the same regardless of field order,
    and `default=str` so a date or Decimal cannot raise inside a producer that
    must never break a submission.
    """
    import hashlib
    import json as _json

    canonical = _json.dumps(values or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.post(
    "/{assignment_id}",
    response_model=FormSubmissionResponse,
)
async def submit_form(
    assignment_id: uuid.UUID,
    body: FormSubmission,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
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
    # Only the assignee, their access group or their N+1 may submit (2026-10-28).
    case_row = await repo.get_case_instance(session, assignment.case_id)
    if case_row is None:
        raise HTTPException(status_code=404, detail="Case not found")
    await require_step_submit(session, user, case_row, assignment)
    if not acts_as(user, body.completed_by):
        raise HTTPException(status_code=403, detail="You can only submit in your own name.")

    # 2. Load and validate form. When the step names a form, that form decides
    # (2026-10-28): the caller chose `form_id`, so a lenient form could be named
    # to skip the check the step's form makes.
    form = await repo.get_form(session, body.form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    step_form_id = await _step_form_id(session, case_row, assignment.step_id)
    if step_form_id and str(step_form_id) != str(body.form_id):
        raise HTTPException(status_code=422, detail={
            "message": "This is not the form of this step.",
            "validation_errors": ["This is not the form of this step."], "field_errors": []})

    # The version this case uses (§5 step 6): its snapshot when the form is
    # versioned per case, else the current form, as before.
    definition = await form_for_case(session, form, assignment.case_id)
    # Field-level security (§5 step 7): a field this caller may not change is
    # read-only for the check, so what was sent for it is dropped.
    definition = secure_definition(definition, await caller_roles(session, user, case_row), for_browser=False)
    # Messages in the caller's language when the form has it (§5 step 7).
    definition = for_caller(definition, request.headers.get("accept-language"))
    context = await case_context(session, user, assignment.case_id)
    values, field_errors = validate_form(
        definition, body.values, context=context,
        references=await form_references(session, definition,
                                         await reference_tenant(session, user, case_row)),
        # Pickers (batch M): only ids this caller may choose, for this case.
        lookups=await picker_lookups(session, definition, body.values,
                                     user=user, case_id=assignment.case_id))
    # Only the form's own keys reach the case (2026-10-28 audit): a submission
    # could otherwise write any key — "approved", "status" — into case data.
    keys = form_keys(definition)
    values = {k: v for k, v in values.items() if k in keys}
    if field_errors:
        legacy = [e.message for e in field_errors]
        raise HTTPException(status_code=422, detail={
            "message": "; ".join(legacy),
            "validation_errors": legacy,
            "field_errors": [e.as_dict() for e in field_errors],
        })

    # 3. Merge form data into case
    case = await repo.get_case_instance(session, assignment.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    merged_data = dict(case.data or {})
    form_namespace = f"form_{str(body.form_id).replace('-', '_')[:8]}"
    merged_data[form_namespace] = values
    # Also put values at top level for convenience
    merged_data.update(values)

    await repo.update_case_instance(
        session, assignment.case_id, values={"data": merged_data}
    )

    # The caller's draft of this form is done with (§5 step 7).
    await drop_draft(session, assignment.case_id, body.form_id, user)

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
            "actor_id": user.user_id,
            "details": {
                "assignment_id": str(assignment_id),
                "form_id": str(body.form_id),
                "step_id": assignment.step_id,
                "field_count": len(values),
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
                        "completed_by": user.user_id,
                        "form_data": values,
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

    # HxShield producer (roadmap #25). `replay_attack` matches on `form_submit`
    # and nothing had ever written it. The pattern hashes the payload to spot a
    # resubmitted form, so the values go into `details` — that hash is the whole
    # signal, and without it the rule has nothing to compare.
    try:
        from velaris_shared.db.security_events import log_security_event
        await log_security_event(
            session, event_type="form_submit", severity="info",
            user_id=getattr(assignment, "assignee_id", None),
            resource_type="form", resource_id=str(assignment_id),
            action="submit", outcome="success",
            ip_address=request.client.host if request.client else None,
            # A FINGERPRINT of the payload, never the payload.
            #
            # `replay_attack` needs to know whether the same form came back, and
            # a hash answers that exactly as well as the values do. Copying the
            # values would put submitted form fields — a customer's answers,
            # which is where personal data lives — into `security_events.details`
            # and from there into `shield_events.raw_context`. Neither is a place
            # HxPrivacy classifies or an erasure request reaches: retention
            # deletes security events by AGE only, and touches `shield_events`
            # not at all. A detection signal must not become an uncontrolled
            # second copy of case data.
            details={"case_id": str(case.id) if case else None,
                     "values_sha256": _values_fingerprint(values),
                     "field_count": len(values or {})},
        )
    except Exception as _e:  # noqa: BLE001
        logger.warning("form_submit security event not recorded: %s", _e)

    return FormSubmissionResponse(
        assignment_id=assignment_id,
        case_id=assignment.case_id,
        form_id=body.form_id,
        values=values,
        status="completed",
    )


@router.get("/{assignment_id}/form")
async def get_assignment_form(
    assignment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get the form definition linked to an assignment's step.

    Looks up the step's form_id from the case type definition.
    """
    assignment = await repo.get_assignment(session, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")

    case = await repo.get_case_instance(session, assignment.case_id)
    # The case must be one the caller can see, as on `GET /cases/{id}/forms/{id}`
    # (2026-10-29): this route now reads the caller's access roles for it.
    from velaris_shared.tenancy.identity import case_visible_to, is_superadmin
    if case is None or not await case_visible_to(session, case.tenant_id, getattr(user, "tenant_id", None),
                                                 is_superadmin_caller=is_superadmin(user)):
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
            # The case's own version when the form is versioned per case (§5 step 6).
            # Without the fields this caller may not see; read-only where they
            # may not change them (field-level security, §5 step 7).
            "definition_json": secure_definition(await form_for_case(session, form, assignment.case_id),
                                                 await caller_roles(session, user, case), for_browser=True),
        },
    }


@router.get("/cases/{case_id}/forms/{form_id}")
async def get_case_form(
    case_id: uuid.UUID,
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """A form as this case uses it (Case Manager): its pinned version when the
    form is versioned per case, else the current form. The case must be one
    the caller can see; a form of another tenant is not served."""
    from velaris_shared.tenancy.identity import case_visible_to, is_superadmin, same_tenant
    case = await repo.get_case_instance(session, case_id)
    if case is None or not await case_visible_to(session, case.tenant_id, getattr(user, "tenant_id", None),
                                                 is_superadmin_caller=is_superadmin(user)):
        raise HTTPException(status_code=404, detail="Case not found")
    form = await repo.get_form(session, form_id)
    if form is None or (form.tenant_id is not None and not is_superadmin(user)
                        and not await same_tenant(session, form.tenant_id, getattr(user, "tenant_id", None))):
        raise HTTPException(status_code=404, detail="Form not found")
    return {"id": str(form.id), "name": form.name, "version": form.version,
            "definition_json": secure_definition(await form_for_case(session, form, case_id),
                                                 await caller_roles(session, user, case), for_browser=True)}


# ── Drafts (Form Builder §5 step 7, migration 227) ────────────────────────────

#: A draft is answers typed so far, not a document store.
DRAFT_MAX_BYTES = 256_000


class DraftIn(BaseModel):
    values: dict[str, Any]


async def _draft_target(session: AsyncSession, user: Any, case_id: uuid.UUID, form_id: uuid.UUID):
    """The case and form a draft is for, refused as a submission would be: the
    case must be one the caller sees, the form of its tenant (or platform), and
    the caller one who may complete work on it — the step's assignee, their
    access group or N+1 (the step whose form this is), else a covering group."""
    from velaris_shared.db.models import CaseAssignmentModel
    from velaris_shared.tenancy.identity import case_visible_to, is_superadmin, same_tenant
    from sqlalchemy import select
    case = await repo.get_case_instance(session, case_id)
    if case is None or not await case_visible_to(session, case.tenant_id, getattr(user, "tenant_id", None),
                                                 is_superadmin_caller=is_superadmin(user)):
        raise HTTPException(status_code=404, detail="Case not found")
    form = await repo.get_form(session, form_id)
    if form is None or (form.tenant_id is not None and not is_superadmin(user)
                        and not await same_tenant(session, form.tenant_id, case.tenant_id)):
        raise HTTPException(status_code=404, detail="Form not found")
    # Only a form one of the case's steps uses: a draft is unfinished work on
    # this case, never a place to store answers to an unrelated form.
    from velaris_shared.db.models import CaseTypeModel
    ct = await session.get(CaseTypeModel, case.case_type_id)
    step_forms = {str(st.get("form_id")) for sg in ((ct.definition_json or {}) if ct else {}).get("stages", []) or []
                  for st in sg.get("steps", []) or [] if st.get("form_id")}
    if str(form_id) not in step_forms:
        raise HTTPException(status_code=404, detail="This case has no such form")
    assignment = None
    for a in (await session.execute(select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id, CaseAssignmentModel.status == "active"))).scalars():
        if str(await _step_form_id(session, case, a.step_id) or "") == str(form_id):
            assignment = a
            break
    await require_step_submit(session, user, case, assignment)
    return case, form


def _draft_owner(user: Any) -> str:
    return str(user.user_id)


@router.get("/cases/{case_id}/forms/{form_id}/draft")
async def get_draft(
    case_id: uuid.UUID,
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The caller's own draft of this case's form; `{"draft": null}` when none."""
    from velaris_shared.db.models import FormDraftModel
    await _draft_target(session, user, case_id, form_id)
    row = await session.get(FormDraftModel, (case_id, form_id, _draft_owner(user)))
    if row is None:
        return {"draft": None}
    return {"draft": {"values": row.values_json or {},
                      "updated_at": row.updated_at.isoformat() if row.updated_at else None}}


@router.put("/cases/{case_id}/forms/{form_id}/draft")
async def save_draft(
    case_id: uuid.UUID,
    form_id: uuid.UUID,
    body: DraftIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Keep the answers so far, unchecked. Only the form's keys the caller may
    change are kept — a draft is never a way to stage another key."""
    import json as _json
    from velaris_shared.db.models import FormDraftModel
    from velaris_shared.forms.security import EDIT, field_access
    from velaris_shared.forms.validate import STATIC_TYPES, _fields
    case, form = await _draft_target(session, user, case_id, form_id)
    definition = await form_for_case(session, form, case_id)
    roles = await caller_roles(session, user, case)
    keys = {field_key_of(f) for f in _fields(definition)
            if f.get("type") not in STATIC_TYPES and not f.get("hidden") and not f.get("read_only")
            and not (isinstance(f.get("calculation"), str) and f["calculation"].strip())
            and field_access(f, roles) == EDIT}
    values = {k: v for k, v in body.values.items() if k in keys}
    if len(_json.dumps(values, default=str)) > DRAFT_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"A draft holds at most {DRAFT_MAX_BYTES // 1000} KB.")
    row = await session.get(FormDraftModel, (case_id, form_id, _draft_owner(user)))
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(FormDraftModel(case_id=case_id, form_id=form_id, user_id=_draft_owner(user),
                                   values_json=values, updated_at=now))
    else:
        row.values_json = values
        row.updated_at = now
    await session.commit()
    return {"draft": {"values": values, "updated_at": now.isoformat()}}


@router.delete("/cases/{case_id}/forms/{form_id}/draft", status_code=204)
async def discard_draft(
    case_id: uuid.UUID,
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Throw the caller's draft away (no draft: nothing to do)."""
    await _draft_target(session, user, case_id, form_id)
    await drop_draft(session, case_id, form_id, user)
    await session.commit()


async def drop_draft(session: AsyncSession, case_id: Any, form_id: Any, user: Any) -> None:
    """The caller's draft is done with — they submitted the form."""
    from sqlalchemy import delete
    from velaris_shared.db.models import FormDraftModel
    try:
        await session.execute(delete(FormDraftModel).where(
            FormDraftModel.case_id == uuid.UUID(str(case_id)), FormDraftModel.form_id == uuid.UUID(str(form_id)),
            FormDraftModel.user_id == _draft_owner(user)))
    except ValueError:
        pass
