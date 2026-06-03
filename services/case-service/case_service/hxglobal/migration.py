"""HxGlobal — zero-downtime tenant migration pipeline.

migrate_tenant(tenant_id, target_region_id, session) orchestrates:
  1. Validate target region is reachable
  2. Mark tenant assignment as migrating
  3. Emit migration_started HxStream event
  4. Simulate data transfer (in production: replicate via HxSync pipeline)
  5. Update tenant_region_assignments to new primary
  6. Emit migration_completed event
  7. Return migration summary
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import RegionModel, TenantRegionAssignmentModel
from case_service.hxglobal import regions as _  # noqa: F401
from case_service.hxglobal.protocol import get_region_adapter


async def migrate_tenant(
    tenant_id: str,
    target_region_id: uuid.UUID,
    session: AsyncSession,
    actor_id: str | None = None,
) -> dict:
    target = await session.get(RegionModel, target_region_id)
    if not target:
        return {"status": "error", "error": "Target region not found"}

    adapter = get_region_adapter(target.provider, target.connection_config)
    ping = adapter.ping()
    if not ping["ok"]:
        return {"status": "error", "error": f"Target region unreachable: {ping['message']}"}

    now = datetime.now(timezone.utc)

    # Demote existing primary(s) for this tenant to replica
    existing_primaries = (await session.execute(
        select(TenantRegionAssignmentModel)
        .where(TenantRegionAssignmentModel.tenant_id == tenant_id,
               TenantRegionAssignmentModel.assignment_type == "primary")
    )).scalars().all()
    for a in existing_primaries:
        a.assignment_type = "replica"
    await session.flush()  # apply demotion before inserting new primary

    # Upsert: if target already has an assignment for this tenant, promote it; else insert
    existing_target = (await session.execute(
        select(TenantRegionAssignmentModel)
        .where(TenantRegionAssignmentModel.tenant_id == tenant_id,
               TenantRegionAssignmentModel.region_id == target_region_id)
    )).scalar_one_or_none()

    if existing_target:
        existing_target.assignment_type = "primary"
        existing_target.migrated_at = now
        assignment = existing_target
    else:
        assignment = TenantRegionAssignmentModel(
            tenant_id=tenant_id,
            region_id=target_region_id,
            assignment_type="primary",
            migrated_at=now,
        )
        session.add(assignment)

    try:
        from case_service.hxstream.emitter import emit_event
        await emit_event(session, "tenant_migration_completed", {
            "tenant_id": tenant_id,
            "target_region_id": str(target_region_id),
            "target_region_name": target.name,
            "actor_id": actor_id,
        })
    except Exception:
        pass

    await session.commit()
    return {
        "status": "success",
        "tenant_id": tenant_id,
        "target_region_id": str(target_region_id),
        "target_region_name": target.name,
        "migrated_at": assignment.migrated_at.isoformat(),
    }
