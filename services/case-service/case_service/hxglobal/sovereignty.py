"""HxGlobal — sovereignty enforcement helpers.

resolve_region(tenant_id, case_type_id, session) → RegionModel | None
Returns the authoritative region for a given tenant/case-type combination
by checking sovereignty_rules, then tenant_region_assignments, then primary.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    RegionModel,
    SovereigntyRuleModel,
    TenantRegionAssignmentModel,
)


async def resolve_region(
    tenant_id: str | None,
    case_type_id: str | None,
    session: AsyncSession,
) -> RegionModel | None:
    """Return the sovereign region for this tenant + case type, or None."""
    # 1. Specific sovereignty rule (tenant + case type)
    if tenant_id and case_type_id:
        rule = (await session.execute(
            select(SovereigntyRuleModel)
            .where(SovereigntyRuleModel.tenant_id == tenant_id,
                   SovereigntyRuleModel.case_type_id == case_type_id)
            .limit(1)
        )).scalar_one_or_none()
        if rule:
            return await session.get(RegionModel, rule.region_id)

    # 2. Tenant-wide sovereignty rule
    if tenant_id:
        rule = (await session.execute(
            select(SovereigntyRuleModel)
            .where(SovereigntyRuleModel.tenant_id == tenant_id,
                   SovereigntyRuleModel.case_type_id == None)  # noqa: E711
            .limit(1)
        )).scalar_one_or_none()
        if rule:
            return await session.get(RegionModel, rule.region_id)

    # 3. Tenant primary assignment
    if tenant_id:
        assignment = (await session.execute(
            select(TenantRegionAssignmentModel)
            .where(TenantRegionAssignmentModel.tenant_id == tenant_id,
                   TenantRegionAssignmentModel.assignment_type == "primary")
            .limit(1)
        )).scalar_one_or_none()
        if assignment:
            return await session.get(RegionModel, assignment.region_id)

    # 4. Global primary region
    return (await session.execute(
        select(RegionModel).where(RegionModel.is_primary == True, RegionModel.enabled == True)  # noqa: E712
        .limit(1)
    )).scalar_one_or_none()


async def log_access(
    region_id,
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    actor_id: str | None = None,
    action: str,
    resource: str | None = None,
    legal_basis: str | None = None,
) -> None:
    from case_service.db.models import RegionAccessLogModel
    session.add(RegionAccessLogModel(
        region_id=region_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        resource=resource,
        legal_basis=legal_basis,
    ))
