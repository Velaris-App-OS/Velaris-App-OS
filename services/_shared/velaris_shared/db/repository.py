"""Case-service repository: async CRUD operations.

Thin data-access layer over SQLAlchemy models.  Business logic
belongs in ``core/`` — this module only handles persistence.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, true, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from velaris_shared.db.models import (
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
    session: AsyncSession, name: str, version: str | None = None,
    *, tenant_key: str | None = None,
) -> CaseTypeModel | None:
    """By name (+ version). `tenant_key` limits it to one owner's namespace —
    `case_type_tenant_key(tenant_id)`, 'platform' for the defaults (8i); omitted,
    every owner, as before."""
    stmt = select(CaseTypeModel).where(CaseTypeModel.name == name)
    if tenant_key is not None:
        stmt = stmt.where(CaseTypeModel.tenant_key == tenant_key)
    if version:
        stmt = stmt.where(CaseTypeModel.version == version)
    else:
        stmt = stmt.order_by(CaseTypeModel.created_at.desc())
    result = await session.execute(stmt)
    return result.scalars().first()


def case_types_visible_to(tenant_id: uuid.UUID):
    """SQL: the case types `tenant_id` can use (Oct 8i).

    Its own rows, plus every default (NULL tenant) it has not FORKED. A fork
    records the exact default it was copied from, and shadows only that row —
    not every default of the same name, because a tenant-owned row of the same
    name can also be a staged-rollout candidate (item 18a) that must sit BESIDE
    its baseline, not hide it.
    """
    from sqlalchemy import and_, exists, or_
    from sqlalchemy.orm import aliased

    own = aliased(CaseTypeModel)
    forked = exists(
        select(own.id).where(
            own.tenant_id == tenant_id,
            own.forked_from_id == CaseTypeModel.id,
            (own.is_deleted == False) | (own.is_deleted == None),  # noqa: E712
        )
    )
    return or_(
        CaseTypeModel.tenant_id == tenant_id,
        and_(CaseTypeModel.tenant_id.is_(None), ~forked),
    )


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
        # Return case types this tenant can USE: their own + the defaults it
        # has not forked (Oct 8i).
        base_filter.append(case_types_visible_to(tenant_id))

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
    """Load one case by id.

    **Deliberately does NOT filter soft-deleted rows.** This is the primary-key
    read used by the dispatcher, by every internal helper and by the restore
    path itself, all of which need to see a deleted case in order to act on it
    or to refuse. The hiding is done where a case is SHOWN — `_get_case_or_404`
    on the API surface and the `is_deleted` clause in `search_case_instances` —
    which is the same division the tenant clause uses, and for the same reason:
    a helper that silently returned None for a row that exists would turn every
    "not found" into a guess.
    """
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
    tenant_id: Any = None,
    include_untenanted: bool = True,
    include_deleted: bool = False,
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

    ``tenant_id`` scopes the search to one tenant. **None means unscoped, which
    is what every release up to now did** — this function had no tenant clause
    at all, so `GET /cases` returned every tenant's cases to every caller.
    Callers that know their tenant pass it; the default is unchanged behaviour
    so no internal caller changes meaning by upgrading.

    ``include_untenanted`` decides whether rows with a NULL tenant come back
    too. It defaults to True because a NULL tenant has always meant
    "platform-shared", and flipping that silently would hide every legacy case
    from everybody. It is set False only by an operator who has run the
    197 backfill and turned `VELARIS_CASE_TENANT_STRICT_READS` on.
    """
    base = select(CaseInstanceModel)
    count_base = select(func.count()).select_from(CaseInstanceModel)

    # 199 — soft-deleted cases are absent from every list, and from the COUNT.
    #
    # Applied to both for the same reason the tenant scope is: a count that
    # ignores the filter reports a total the caller cannot page to, which makes
    # pagination lie and tells them how many hidden cases there are.
    #
    # `include_deleted` exists for exactly one caller — the restore path, which
    # has to be able to find what it is restoring. Everything else gets the
    # filter whether it asks or not, because a soft delete that any query can
    # forget is not a delete.
    if not include_deleted:
        base = base.where(CaseInstanceModel.is_deleted.is_(False))
        count_base = count_base.where(CaseInstanceModel.is_deleted.is_(False))

    # Tenant scope. Applied to BOTH the page and the count — a count that
    # ignores the filter reports a total the caller may not read, which is a
    # smaller leak of the same kind and makes pagination lie.
    if tenant_id is not None:
        from sqlalchemy import or_ as _or
        scope = CaseInstanceModel.tenant_id == tenant_id
        if include_untenanted:
            scope = _or(scope, CaseInstanceModel.tenant_id.is_(None))
        base = base.where(scope)
        count_base = count_base.where(scope)

    for key, val in filters.items():
        if hasattr(CaseInstanceModel, key):
            col = getattr(CaseInstanceModel, key)
            base = base.where(col == val)
            count_base = count_base.where(col == val)

    if accessible_to_user is not None:
        # HxGuard enforce mode: restrict the list to cases the user has a
        # relationship with — owner, tuple (assignee/viewer/editor), or an
        # access-group-allowed case type. Mirrors RebacBackend.evaluate.
        from velaris_shared.db.models import HxGuardTupleModel as _HXT
        uid, allowed_type_ids = accessible_to_user
        from sqlalchemy import String as _Str, cast as _cast
        from velaris_shared.db.models import (
            AccessGroupModel as _AG, OperatorAccessGroupModel as _OAG,
        )

        # Shape (R6 soak, 2026-11-02): `id IN (one indexed lookup per way in)`,
        # never `col = x OR EXISTS(…) OR EXISTS(…)`. The OR form could only be
        # answered by reading EVERY case in the tenant and probing each one —
        # 28,000 rows read to return a worker's 470, twice per list (page +
        # count) — so a list got slower with every case anyone created, and
        # at 400 users PostgreSQL took the box (p95 17 s). Each branch below
        # is an index lookup (created_by: migration 228; tuples: subject
        # index; case type: idx_cases_type); the result is the same set.
        from sqlalchemy import union_all
        clauses = [
            select(CaseInstanceModel.id).where(CaseInstanceModel.created_by == uid),
            select(_HXT.object_id).where(
                _HXT.object_type == "case",
                _HXT.subject_type == "user",
                _HXT.subject_id == uid,
                _HXT.relation.in_(("assignee", "viewer", "editor")),
            ),
            # A tuple against an ACCESS GROUP this operator actively belongs to.
            #
            # This clause is why the comment above says "mirrors
            # RebacBackend.evaluate" and still means it. `transfer` (2026-09-08)
            # can hand a case to a group and the backend resolves that through
            # membership — so without this the case would be readable BY ID and
            # ABSENT FROM THE LIST, which is the worst of both: the operator is
            # told the case does not exist right up until somebody sends them a
            # link to it.
            #
            # `cast(_AG.id, String)` is load-bearing, not tidiness.
            # `hxguard_tuples.subject_id` is a `String(255)` — it holds user
            # ids, group ids and anything else a subject may be — while
            # `access_groups.id` is a `GUID`. On PostgreSQL an uncast
            # `varchar IN (SELECT uuid)` is a type error; on SQLite it silently
            # matches nothing, which is the worse of the two because the feature
            # would look built and quietly grant nobody anything. This is the
            # THIRD form of the same mismatch in this codebase — after
            # `costing._authorized_case` (GUID vs slug) and the access-group
            # tenant check earlier today.
            select(_HXT.object_id).where(
                _HXT.object_type == "case",
                _HXT.subject_type == "access_group",
                _HXT.relation.in_(("assignee", "viewer", "editor")),
                _HXT.subject_id.in_(
                    select(_cast(_AG.id, _Str))
                    .join(_OAG, _OAG.access_group_id == _AG.id)
                    .where(_OAG.operator_id == uid)
                    .where(_AG.is_active == True)  # noqa: E712
                ),
            ),
        ]
        if allowed_type_ids:
            clauses.append(select(CaseInstanceModel.id)
                           .where(CaseInstanceModel.case_type_id.in_(allowed_type_ids)))
        cond = CaseInstanceModel.id.in_(union_all(*clauses))
        base = base.where(cond)
        count_base = count_base.where(cond)

    if variable_filters:
        from sqlalchemy import exists, or_
        from velaris_shared.db.models import CaseInstanceVariableModel as _CIV
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
        from velaris_shared.hxguard import tuples as hxg_tuples
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
    where=None,
) -> tuple[list[DataModelModel], int]:
    """`where` narrows the rows (8e: the caller's tenant + inherited defaults)."""
    count_stmt = select(func.count()).select_from(DataModelModel)
    if where is not None:
        count_stmt = count_stmt.where(where)
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        select(DataModelModel)
        .where(where if where is not None else true())
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
    where=None,
) -> tuple[list[FormDefinitionModel], int]:
    """`where` narrows the rows (8e: the caller's tenant + inherited defaults)."""
    count_stmt = select(func.count()).select_from(FormDefinitionModel)
    if where is not None:
        count_stmt = count_stmt.where(where)
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        select(FormDefinitionModel)
        .where(where if where is not None else true())
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
    where=None,
) -> tuple[list[RuleDefinitionModel], int]:
    """`where` narrows the rows (8e: the caller's tenant + inherited defaults)."""
    base = select(RuleDefinitionModel)
    count_base = select(func.count()).select_from(RuleDefinitionModel)
    if where is not None:
        base = base.where(where)
        count_base = count_base.where(where)

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

from velaris_shared.db.models import BusinessCalendarModel


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
    *,
    where=None,
) -> list[BusinessCalendarModel]:
    stmt = select(BusinessCalendarModel).order_by(BusinessCalendarModel.name)
    if where is not None:
        stmt = stmt.where(where)
    result = await session.execute(stmt)
    return list(result.scalars().all())
