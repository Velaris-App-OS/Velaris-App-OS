"""Case-service repository: async CRUD operations.

Thin data-access layer over SQLAlchemy models.  Business logic
belongs in ``core/`` — this module only handles persistence.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from case_service.db.models import (
    CaseAssignmentModel,
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseRelationshipModel,
    CaseSLAInstanceModel,
    CaseTypeModel,
    CaseTypeStageModel,
    DataModelModel,
    FormDefinitionModel,
    RuleDefinitionModel,
    WorkQueueModel,
)


# ─── Case Types ───────────────────────────────────────────────────────


async def create_case_type(
    session: AsyncSession, *, data: dict[str, Any]
) -> CaseTypeModel:
    model = CaseTypeModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_case_type(
    session: AsyncSession, case_type_id: uuid.UUID
) -> CaseTypeModel | None:
    stmt = (
        select(CaseTypeModel)
        .where(CaseTypeModel.id == case_type_id)
        .options(selectinload(CaseTypeModel.stages))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_case_type_by_name(
    session: AsyncSession, name: str, version: str | None = None
) -> CaseTypeModel | None:
    stmt = select(CaseTypeModel).where(CaseTypeModel.name == name)
    if version:
        stmt = stmt.where(CaseTypeModel.version == version)
    else:
        stmt = stmt.order_by(CaseTypeModel.created_at.desc())
    result = await session.execute(stmt)
    return result.scalars().first()


async def list_case_types(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 50,
    include_deleted: bool = False,
    tenant_id: uuid.UUID | None = None,
) -> tuple[list[CaseTypeModel], int]:
    base_filter = []
    if not include_deleted and hasattr(CaseTypeModel, "is_deleted"):
        base_filter.append(
            (CaseTypeModel.is_deleted == False) | (CaseTypeModel.is_deleted == None)  # noqa: E712
        )
    if tenant_id is not None:
        # Return case types this tenant can USE: their own + platform-wide globals
        from sqlalchemy import or_
        base_filter.append(
            or_(CaseTypeModel.tenant_id == tenant_id, CaseTypeModel.tenant_id.is_(None))
        )

    count_stmt = select(func.count()).select_from(CaseTypeModel)
    for f in base_filter:
        count_stmt = count_stmt.where(f)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = select(CaseTypeModel)
    for f in base_filter:
        stmt = stmt.where(f)
    stmt = stmt.order_by(CaseTypeModel.name, CaseTypeModel.version).offset(offset).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


async def delete_case_type(
    session: AsyncSession, case_type_id: uuid.UUID
) -> bool:
    stmt = delete(CaseTypeModel).where(CaseTypeModel.id == case_type_id)
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


# ─── Case Instances ───────────────────────────────────────────────────


async def create_case_instance(
    session: AsyncSession, *, data: dict[str, Any]
) -> CaseInstanceModel:
    model = CaseInstanceModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_case_instance(
    session: AsyncSession,
    case_id: uuid.UUID,
    *,
    load_assignments: bool = False,
    load_sla: bool = False,
    load_audit: bool = False,
) -> CaseInstanceModel | None:
    stmt = select(CaseInstanceModel).where(
        CaseInstanceModel.id == case_id
    )
    if load_assignments:
        stmt = stmt.options(
            selectinload(CaseInstanceModel.assignments)
        )
    if load_sla:
        stmt = stmt.options(
            selectinload(CaseInstanceModel.sla_instances)
        )
    if load_audit:
        stmt = stmt.options(
            selectinload(CaseInstanceModel.audit_entries)
        )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def update_case_instance(
    session: AsyncSession,
    case_id: uuid.UUID,
    *,
    values: dict[str, Any],
) -> bool:
    values["updated_at"] = datetime.now(timezone.utc)
    stmt = (
        update(CaseInstanceModel)
        .where(CaseInstanceModel.id == case_id)
        .values(**values)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def search_case_instances(
    session: AsyncSession,
    *,
    filters: dict[str, Any],
    offset: int = 0,
    limit: int = 50,
    variable_filters: list[tuple[str, str]] | None = None,
    accessible_to_user: tuple[str, list] | None = None,
) -> tuple[list[CaseInstanceModel], int]:
    """Search cases with simple equality filters on top-level columns.

    ``variable_filters`` (Case Variables Phase 3): (full_key, raw_value)
    pairs matched against case_instance_variables via EXISTS subqueries on
    the fixed (full_key, value_*) indexes from migration 087. AND semantics
    across pairs. Callers enforce the indexed-only policy — this function
    only builds SQL (never DDL, never string interpolation).

    ``accessible_to_user`` (HxGuard enforce cutover): (user_id,
    allowed_case_type_ids) — restricts results to owner OR relationship
    tuple OR allowed case types. None = unrestricted (admin/manager, "*"
    scope, or shadow/off mode).
    """
    base = select(CaseInstanceModel)
    count_base = select(func.count()).select_from(CaseInstanceModel)

    for key, val in filters.items():
        if hasattr(CaseInstanceModel, key):
            col = getattr(CaseInstanceModel, key)
            base = base.where(col == val)
            count_base = count_base.where(col == val)

    if accessible_to_user is not None:
        # HxGuard enforce mode: restrict the list to cases the user has a
        # relationship with — owner, tuple (assignee/viewer/editor), or an
        # access-group-allowed case type. Mirrors RebacBackend.evaluate.
        from sqlalchemy import exists, or_
        from case_service.db.models import HxGuardTupleModel as _HXT
        uid, allowed_type_ids = accessible_to_user
        clauses = [
            CaseInstanceModel.created_by == uid,
            exists().where(
                _HXT.object_type == "case",
                _HXT.object_id == CaseInstanceModel.id,
                _HXT.subject_type == "user",
                _HXT.subject_id == uid,
                _HXT.relation.in_(("assignee", "viewer", "editor")),
            ),
        ]
        if allowed_type_ids:
            clauses.append(CaseInstanceModel.case_type_id.in_(allowed_type_ids))
        cond = or_(*clauses)
        base = base.where(cond)
        count_base = count_base.where(cond)

    if variable_filters:
        from sqlalchemy import exists, or_
        from case_service.db.models import CaseInstanceVariableModel as _CIV
        for full_key, raw in variable_filters:
            value_clauses = [_CIV.value_text == raw]
            try:
                value_clauses.append(_CIV.value_num == float(raw))
            except (TypeError, ValueError):
                pass
            if raw.lower() in ("true", "false"):
                value_clauses.append(_CIV.value_bool == (raw.lower() == "true"))
            cond = exists().where(
                _CIV.case_id == CaseInstanceModel.id,
                _CIV.full_key == full_key,
                or_(*value_clauses),
            )
            base = base.where(cond)
            count_base = count_base.where(cond)

    total = (await session.execute(count_base)).scalar_one()
    stmt = base.order_by(
        CaseInstanceModel.urgency_score.desc(),
        CaseInstanceModel.created_at.desc(),
    ).offset(offset).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


# ─── Assignments ──────────────────────────────────────────────────────


async def create_assignment(
    session: AsyncSession, *, data: dict[str, Any]
) -> CaseAssignmentModel:
    model = CaseAssignmentModel(**data)
    session.add(model)
    await session.flush()
    # HxGuard Phase B: user assignments materialize an assignee tuple in the
    # SAME transaction — authz can't diverge from assignment state.
    if model.assignee_type == "user" and model.assignee_id:
        from case_service.hxguard import tuples as hxg_tuples
        await hxg_tuples.write_tuple(
            session, object_type="case", object_id=model.case_id,
            relation="assignee", subject_type="user",
            subject_id=str(model.assignee_id), created_by="assignment",
        )
    return model


async def get_assignment(
    session: AsyncSession, assignment_id: uuid.UUID
) -> CaseAssignmentModel | None:
    stmt = select(CaseAssignmentModel).where(
        CaseAssignmentModel.id == assignment_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def update_assignment(
    session: AsyncSession,
    assignment_id: uuid.UUID,
    *,
    values: dict[str, Any],
) -> bool:
    stmt = (
        update(CaseAssignmentModel)
        .where(CaseAssignmentModel.id == assignment_id)
        .values(**values)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def get_assignments_for_user(
    session: AsyncSession,
    user_id: str,
    *,
    status: str = "active",
) -> list[CaseAssignmentModel]:
    stmt = (
        select(CaseAssignmentModel)
        .where(
            CaseAssignmentModel.assignee_id == user_id,
            CaseAssignmentModel.status == status,
        )
        .order_by(CaseAssignmentModel.assigned_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Relationships ────────────────────────────────────────────────────


async def create_relationship(
    session: AsyncSession, *, data: dict[str, Any]
) -> CaseRelationshipModel:
    model = CaseRelationshipModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_relationships(
    session: AsyncSession, case_id: uuid.UUID
) -> list[CaseRelationshipModel]:
    stmt = select(CaseRelationshipModel).where(
        (CaseRelationshipModel.source_case_id == case_id)
        | (CaseRelationshipModel.target_case_id == case_id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── SLA Instances ────────────────────────────────────────────────────


async def create_sla_instance(
    session: AsyncSession, *, data: dict[str, Any]
) -> CaseSLAInstanceModel:
    model = CaseSLAInstanceModel(**data)
    session.add(model)
    await session.flush()
    return model


async def update_sla_instance(
    session: AsyncSession,
    sla_id: uuid.UUID,
    *,
    values: dict[str, Any],
) -> bool:
    stmt = (
        update(CaseSLAInstanceModel)
        .where(CaseSLAInstanceModel.id == sla_id)
        .values(**values)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def get_sla_instances(
    session: AsyncSession, case_id: uuid.UUID
) -> list[CaseSLAInstanceModel]:
    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Audit Log ────────────────────────────────────────────────────────


async def append_audit_entry(
    session: AsyncSession, *, data: dict[str, Any]
) -> CaseAuditLogModel:
    model = CaseAuditLogModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_audit_log(
    session: AsyncSession,
    case_id: uuid.UUID,
    *,
    offset: int = 0,
    limit: int = 100,
) -> list[CaseAuditLogModel]:
    stmt = (
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.case_id == case_id)
        .order_by(CaseAuditLogModel.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Work Queues ──────────────────────────────────────────────────────


async def create_work_queue(
    session: AsyncSession, *, data: dict[str, Any]
) -> WorkQueueModel:
    model = WorkQueueModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_work_queue(
    session: AsyncSession, queue_id: uuid.UUID
) -> WorkQueueModel | None:
    stmt = select(WorkQueueModel).where(WorkQueueModel.id == queue_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_work_queues(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
) -> list[WorkQueueModel]:
    stmt = select(WorkQueueModel)
    if tenant_id is not None:
        stmt = stmt.where(WorkQueueModel.tenant_id == tenant_id)
    stmt = stmt.order_by(WorkQueueModel.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Data Models (Phase 3) ────────────────────────────────────────


async def create_data_model(
    session: AsyncSession, *, data: dict[str, Any]
) -> DataModelModel:
    model = DataModelModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_data_model(
    session: AsyncSession, model_id: uuid.UUID
) -> DataModelModel | None:
    stmt = select(DataModelModel).where(DataModelModel.id == model_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_data_model_by_name(
    session: AsyncSession, name: str, version: str | None = None
) -> DataModelModel | None:
    stmt = select(DataModelModel).where(DataModelModel.name == name)
    if version:
        stmt = stmt.where(DataModelModel.version == version)
    else:
        stmt = stmt.order_by(DataModelModel.created_at.desc())
    result = await session.execute(stmt)
    return result.scalars().first()


async def list_data_models(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[DataModelModel], int]:
    count_stmt = select(func.count()).select_from(DataModelModel)
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        select(DataModelModel)
        .order_by(DataModelModel.name, DataModelModel.version)
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


async def update_data_model(
    session: AsyncSession,
    model_id: uuid.UUID,
    *,
    values: dict[str, Any],
) -> bool:
    stmt = (
        update(DataModelModel)
        .where(DataModelModel.id == model_id)
        .values(**values)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


async def delete_data_model(
    session: AsyncSession, model_id: uuid.UUID
) -> bool:
    stmt = delete(DataModelModel).where(DataModelModel.id == model_id)
    result = await session.execute(stmt)
    return result.rowcount > 0


# ─── Form Definitions (Phase 3) ──────────────────────────────────


async def create_form(
    session: AsyncSession, *, data: dict[str, Any]
) -> FormDefinitionModel:
    model = FormDefinitionModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_form(
    session: AsyncSession, form_id: uuid.UUID
) -> FormDefinitionModel | None:
    stmt = select(FormDefinitionModel).where(
        FormDefinitionModel.id == form_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_forms(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[FormDefinitionModel], int]:
    count_stmt = select(func.count()).select_from(FormDefinitionModel)
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        select(FormDefinitionModel)
        .order_by(FormDefinitionModel.name, FormDefinitionModel.version)
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


async def update_form(
    session: AsyncSession,
    form_id: uuid.UUID,
    *,
    values: dict[str, Any],
) -> bool:
    stmt = (
        update(FormDefinitionModel)
        .where(FormDefinitionModel.id == form_id)
        .values(**values)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


async def delete_form(
    session: AsyncSession, form_id: uuid.UUID
) -> bool:
    stmt = delete(FormDefinitionModel).where(
        FormDefinitionModel.id == form_id
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


# ─── Rule Definitions (Phase 3) ──────────────────────────────────


async def create_rule(
    session: AsyncSession, *, data: dict[str, Any]
) -> RuleDefinitionModel:
    model = RuleDefinitionModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_rule(
    session: AsyncSession, rule_id: uuid.UUID
) -> RuleDefinitionModel | None:
    stmt = select(RuleDefinitionModel).where(
        RuleDefinitionModel.id == rule_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_rules(
    session: AsyncSession,
    *,
    rule_type: str | None = None,
    scope: str | None = None,
    enabled: bool | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[RuleDefinitionModel], int]:
    base = select(RuleDefinitionModel)
    count_base = select(func.count()).select_from(RuleDefinitionModel)

    if rule_type:
        base = base.where(RuleDefinitionModel.rule_type == rule_type)
        count_base = count_base.where(RuleDefinitionModel.rule_type == rule_type)
    if scope:
        base = base.where(RuleDefinitionModel.scope == scope)
        count_base = count_base.where(RuleDefinitionModel.scope == scope)
    if enabled is not None:
        base = base.where(RuleDefinitionModel.enabled == enabled)
        count_base = count_base.where(RuleDefinitionModel.enabled == enabled)

    total = (await session.execute(count_base)).scalar_one()
    stmt = (
        base.order_by(
            RuleDefinitionModel.priority.desc(),
            RuleDefinitionModel.name,
        )
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


async def update_rule(
    session: AsyncSession,
    rule_id: uuid.UUID,
    *,
    values: dict[str, Any],
) -> bool:
    stmt = (
        update(RuleDefinitionModel)
        .where(RuleDefinitionModel.id == rule_id)
        .values(**values)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


async def delete_rule(
    session: AsyncSession, rule_id: uuid.UUID
) -> bool:
    stmt = delete(RuleDefinitionModel).where(
        RuleDefinitionModel.id == rule_id
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


# ─── Assignments by case (Phase 6) ───────────────────────────────


async def get_assignments_for_case(
    session: AsyncSession, case_id: uuid.UUID
) -> list[CaseAssignmentModel]:
    stmt = (
        select(CaseAssignmentModel)
        .where(CaseAssignmentModel.case_id == case_id)
        .order_by(CaseAssignmentModel.assigned_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())



# ─── Business Calendar (Phase 8) ─────────────────────────────────

from case_service.db.models import BusinessCalendarModel


async def create_business_calendar(
    session: AsyncSession, *, data: dict[str, Any]
) -> BusinessCalendarModel:
    model = BusinessCalendarModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_business_calendar(
    session: AsyncSession, calendar_id: uuid.UUID
) -> BusinessCalendarModel | None:
    stmt = select(BusinessCalendarModel).where(
        BusinessCalendarModel.id == calendar_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_business_calendar_by_name(
    session: AsyncSession, name: str
) -> BusinessCalendarModel | None:
    stmt = select(BusinessCalendarModel).where(
        BusinessCalendarModel.name == name
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_business_calendars(
    session: AsyncSession,
) -> list[BusinessCalendarModel]:
    stmt = select(BusinessCalendarModel).order_by(BusinessCalendarModel.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())
