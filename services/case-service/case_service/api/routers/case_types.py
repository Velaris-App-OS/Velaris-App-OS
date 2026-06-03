"""Case Type API router.

Handles deployment, listing, updating, and removal of case type definitions.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import update as sa_update, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.cases import (
    CaseTypeCreate,
    CaseTypeListResponse,
    CaseTypeResponse,
)
from case_service.db import repository as repo
from case_service.db.models import CaseTypeModel, CaseTypeMigrationModel
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session

router = APIRouter(prefix="/case-types", tags=["case-types"], dependencies=[Depends(get_current_user)])


@router.post("", response_model=CaseTypeResponse, status_code=201)
async def deploy_case_type(
    body: CaseTypeCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Deploy (register) a new case type definition.

    Access rules:
      - Global case type (tenant_id = None): requires admin + developer role.
      - Tenant-owned case type: requires developer role in that tenant.
    """
    _assert_can_write_case_type(user, body.tenant_id, action="create")

    existing = await repo.get_case_type_by_name(
        session, body.name, body.version
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Case type '{body.name}' v{body.version} already exists",
        )

    ct = await repo.create_case_type(
        session,
        data={
            "name": body.name,
            "version": body.version,
            "tenant_id": body.tenant_id,
            "lifecycle_process_id": body.lifecycle_process_id,
            "data_model_id": body.data_model_id,
            "security_profile_id": body.security_profile_id,
            "default_priority": body.default_priority,
            "definition_json": body.definition_json,
            "icon": body.icon,
            "color": body.color,
            "description": body.description,
            "tags": body.tags,
        },
    )

    return ct


@router.get("", response_model=CaseTypeListResponse)
async def list_case_types(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # Resolve which tenant to filter by:
    #   1. Explicit ?tenant_id param (admin override)
    #   2. Auto-derive from the authenticated user's tenant
    #   3. Superadmin with no tenant → no filter (sees everything)
    effective_tenant_id = tenant_id
    if effective_tenant_id is None and user.tenant_id:
        try:
            effective_tenant_id = uuid.UUID(str(user.tenant_id))
        except (ValueError, AttributeError):
            pass

    items, total = await repo.list_case_types(
        session,
        offset=(page - 1) * page_size,
        limit=page_size,
        tenant_id=effective_tenant_id,   # returns tenant's + globals (tenant_id IS NULL)
    )
    return CaseTypeListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.get("/{case_type_id}", response_model=CaseTypeResponse)
async def get_case_type(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    ct = await repo.get_case_type(session, case_type_id)
    if ct is None:
        raise HTTPException(
            status_code=404, detail="Case type not found"
        )
    return ct


def _assert_can_write_case_type(
    user: AuthenticatedUser,
    case_type_tenant_id: uuid.UUID | None,
    action: str = "update",
) -> None:
    """Raise 403 if the user cannot create or edit this case type.

    Authorization is driven entirely by the privileges configured in the
    Access Directory (access_roles.privileges).  No role name is hardcoded.

    Rules:
      - Global (tenant_id = None):
            needs case_type write privilege
            + admin.manage privilege (platform administration gate)
      - Tenant-owned:
            needs case_type write privilege
            + user must belong to that tenant
    """
    # Superadmin bypasses everything
    if "superadmin" in (user.roles or []):
        return

    can_write       = user.has_privilege("case_type", action)
    can_edit_global = user.has_privilege("case_type", "global.write")

    if case_type_tenant_id is None:
        # Global case type — requires case_type write + case_type.global.write privilege.
        # Admin bypasses automatically.
        # Developers get this via: Access Directory → Access Roles → add privilege
        #   resource: case_type  action: global.write
        # No admin console access is needed or implied.
        if not (user.is_admin or (can_write and can_edit_global)):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Editing global case types requires the 'case_type / global.write' privilege. "
                    "Ask your admin to add it to your access role in "
                    "Admin Console → Access Directory → Access Roles."
                ),
            )
    else:
        # Tenant-owned — privilege + must belong to that tenant
        if not can_write:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Your access role does not grant case_type write privileges. "
                    "Configure this in Access Directory → Access Roles."
                ),
            )
        if str(case_type_tenant_id) != str(user.tenant_id or ""):
            raise HTTPException(
                status_code=403,
                detail="You can only edit case types owned by your tenant.",
            )


@router.patch("/{case_type_id}", response_model=CaseTypeResponse)
async def update_case_type(
    case_type_id: uuid.UUID,
    body: dict[str, Any],
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Update a case type's definition, description, or other fields."""
    ct = await repo.get_case_type(session, case_type_id)
    if ct is None:
        raise HTTPException(
            status_code=404, detail="Case type not found"
        )

    _assert_can_write_case_type(user, ct.tenant_id, action="update")

    allowed = {
        "definition_json", "description", "default_priority",
        "tags", "icon", "color",
        # Intake trigger fields
        "intake_trigger", "trigger_connector_id",
        "filter_conditions", "field_mapping", "process_definition_id",
    }
    values = {k: v for k, v in body.items() if k in allowed}

    if values:
        stmt = (
            sa_update(CaseTypeModel)
            .where(CaseTypeModel.id == case_type_id)
            .values(**values)
        )
        await session.execute(stmt)

    result = await repo.get_case_type(session, case_type_id)

    # Auto-regenerate any linked HxDocs lifecycle articles in the background
    if "definition_json" in values:
        background_tasks.add_task(_sync_lifecycle_docs, case_type_id)

    return result


async def _sync_lifecycle_docs(case_type_id: uuid.UUID) -> None:
    """Background task: find and regenerate all lifecycle articles for this case type."""
    import logging
    log = logging.getLogger(__name__)
    try:
        from case_service.db.session import get_engine
        from sqlalchemy.ext.asyncio import AsyncSession as _Session, async_sessionmaker
        from case_service.hxdocs import service as docs_service
        from case_service.hxstream.emitter import emit_trace

        engine = get_engine()
        factory = async_sessionmaker(engine, class_=_Session, expire_on_commit=False)
        async with factory() as session:
            articles = await docs_service.find_lifecycle_articles_for_case_type(session, case_type_id)
            log.info("lifecycle auto-sync: found %d articles for case_type %s", len(articles), case_type_id)
            updated_ids: list[str] = []
            for article in articles:
                await docs_service.regenerate_lifecycle_article(session, article, actor="system")
                updated_ids.append(str(article.id))
            if articles:
                await session.commit()
                # Emit one event per article so the frontend can match by article_id
                for art_id in updated_ids:
                    await emit_trace(
                        "docs.lifecycle_auto_synced",
                        {"case_type_id": str(case_type_id), "article_id": art_id},
                    )
                log.info("lifecycle auto-sync: emitted events for %s", updated_ids)
            else:
                log.info("lifecycle auto-sync: no articles found for case_type %s", case_type_id)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning("lifecycle auto-sync failed: %s", exc, exc_info=True)


@router.delete("/{case_type_id}", status_code=204)
async def delete_case_type(
    case_type_id: uuid.UUID,
    hard: bool = False,
    session: AsyncSession = Depends(get_session),
):
    """Soft-delete a case type (or hard-delete if ?hard=true).

    Soft-deleted case types are excluded from list by default
    but can be restored via POST /{id}/restore.
    """
    ct = await repo.get_case_type(session, case_type_id)
    if ct is None:
        raise HTTPException(status_code=404, detail="Case type not found")

    if hard:
        await repo.delete_case_type(session, case_type_id)
    else:
        now = datetime.now(timezone.utc)
        stmt = (
            sa_update(CaseTypeModel)
            .where(CaseTypeModel.id == case_type_id)
            .values(is_deleted=True, deleted_at=now)
        )
        await session.execute(stmt)


@router.post("/{case_type_id}/restore", response_model=CaseTypeResponse)
async def restore_case_type(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Restore a soft-deleted case type."""
    ct = await repo.get_case_type(session, case_type_id)
    if ct is None:
        raise HTTPException(status_code=404, detail="Case type not found")

    if not getattr(ct, "is_deleted", False):
        raise HTTPException(status_code=409, detail="Case type is not deleted")

    stmt = (
        sa_update(CaseTypeModel)
        .where(CaseTypeModel.id == case_type_id)
        .values(is_deleted=False, deleted_at=None, deleted_by=None)
    )
    await session.execute(stmt)
    return await repo.get_case_type(session, case_type_id)


# ── Migration History (audit trail) ──────────────────────────────────────────

class MigrationHistoryRecord(BaseModel):
    id:                   uuid.UUID
    case_type_id:         uuid.UUID
    run_id:               uuid.UUID | None
    source_platform:      str
    source_filename:      str
    imported_by_user_id:  str
    imported_by_email:    str
    imported_at:          str
    stages_count:         int
    steps_count:          int
    forms_count:          int
    rules_count:          int
    slas_count:           int
    notes:                str


@router.get("/{case_type_id}/migration-history", response_model=list[MigrationHistoryRecord])
async def get_migration_history(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Return the full import history for a case type.

    Visible to all authenticated users — no special permission required.
    Every HxMigrate import appends an immutable record here.
    """
    rows = (await session.execute(
        select(CaseTypeMigrationModel)
        .where(CaseTypeMigrationModel.case_type_id == case_type_id)
        .order_by(CaseTypeMigrationModel.imported_at.desc())
    )).scalars().all()

    return [
        MigrationHistoryRecord(
            id=r.id,
            case_type_id=r.case_type_id,
            run_id=r.run_id,
            source_platform=r.source_platform,
            source_filename=r.source_filename,
            imported_by_user_id=r.imported_by_user_id,
            imported_by_email=r.imported_by_email,
            imported_at=r.imported_at.isoformat(),
            stages_count=r.stages_count,
            steps_count=r.steps_count,
            forms_count=r.forms_count,
            rules_count=r.rules_count,
            slas_count=r.slas_count,
            notes=r.notes,
        )
        for r in rows
    ]
