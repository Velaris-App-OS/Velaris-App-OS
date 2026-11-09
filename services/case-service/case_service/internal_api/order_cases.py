"""Open the case behind a commerce order — the case half of HxCheckout.

Until the commerce split this ran inline in `checkout.service` (create_order /
create_subcase), in the same transaction as the order. commerce-service now
calls it over the internal API BEFORE it writes the order, so it is idempotent
on `request_key` (table `commerce_case_requests`, migration 220): a retried
call returns the case the first one opened. A case
whose order never got written is closed by `sweep_orphans` (scheduler).

The code below is what checkout ran, unchanged apart from the idempotency check.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.internal_api import order_case_types as ct_svc
from case_service.core import case_lifecycle
from velaris_shared.db import repository as repo
from velaris_shared.db.models import CaseInstanceModel, CommerceCaseRequestModel

logger = logging.getLogger(__name__)

KINDS = ("order", "return", "complaint")


async def _existing(session: AsyncSession, request_key: str) -> CommerceCaseRequestModel | None:
    return await session.get(CommerceCaseRequestModel, request_key)


async def open_case(
    session: AsyncSession, *, kind: str, tenant: str | None, request_key: str,
    data: dict[str, Any], created_by: str, rollout_subject: tuple, via: str,
    parent_case_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Open (or return the already-opened) case for an order / return / complaint."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    found = await _existing(session, request_key)
    if found is not None:
        return {"case_id": str(found.case_id), "created": False}

    if kind == "order":
        case_type = await ct_svc.resolve_order_case_type(session, tenant)
    elif kind == "return":
        case_type = await ct_svc.resolve_return_case_type(session)
    else:
        case_type = await ct_svc.resolve_complaint_case_type(session)
    case_tenant = ct_svc.tenant_uuid(tenant)   # UUID or None — never a bare string

    from case_service.rollouts import service as _rollouts
    case_type, _rollout = await _rollouts.resolve_case_type(
        session, case_type=case_type, tenant=case_tenant,
        subject=_rollouts.subject_of(*rollout_subject))
    row = {
        "case_type_id": case_type.id,
        "case_type_version": case_type.version,
        "status": "new",
        "priority": case_type.default_priority,
        "data": data,
        "created_by": created_by,
        "tenant_id": case_tenant,
    }
    if parent_case_id is not None:
        row["parent_case_id"] = parent_case_id
    case = await repo.create_case_instance(session, data=row)
    await _rollouts.record(session, case_id=case.id, res=_rollout, tenant=case_tenant, via=via)
    session.add(CommerceCaseRequestModel(request_key=request_key, kind=kind, case_id=case.id))

    # Start case-level SLAs (the delivery-estimate ring depends on these). Best-effort.
    try:
        await case_lifecycle.on_case_created(
            session, case_id=case.id, case_type_def=case_type.definition_json or {},
            case_type_id=case.case_type_id, tenant_id=case_tenant,
        )
    except Exception as e:
        logger.warning("SLA start failed for %s case %s: %s", kind, case.id, e)
    return {"case_id": str(case.id), "created": True}


#: The actor the sweep records on the audit trail.
SWEEP_ACTOR = "system:commerce-order-sweep"
#: How long an opened case may wait for its order before the sweep closes it.
ORPHAN_GRACE_MINUTES = 30


async def sweep_orphans(session: AsyncSession) -> int:
    """Close order-cases whose order was never written (commerce's transaction
    failed after the case opened). Returns how many were closed."""
    from datetime import datetime, timedelta, timezone

    from velaris_shared.db.models import CheckoutOrderModel

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ORPHAN_GRACE_MINUTES)
    # Order-cases older than the grace period, still `new`, that no order names.
    rows = (await session.execute(
        select(CaseInstanceModel)
        .join(CommerceCaseRequestModel, CommerceCaseRequestModel.case_id == CaseInstanceModel.id)
        .outerjoin(CheckoutOrderModel, CheckoutOrderModel.case_id == CaseInstanceModel.id)
        .where(
            CommerceCaseRequestModel.kind == "order",
            CommerceCaseRequestModel.created_at < cutoff,
            CaseInstanceModel.status == "new",
            CheckoutOrderModel.id.is_(None),
        ).limit(200)
    )).scalars().all()
    closed = 0
    for case in rows:
        # The same path a "cancel" action takes — transition check, then
        # apply_status_change (audit row, SLA hook, broadcast, outbox) — as a
        # named system actor, so the case never changes silently.
        from case_service.api.routers.cases import apply_status_change
        from case_service.case_actions import statuses

        try:
            await statuses.validate_transition(
                session, current=case.status, target="cancelled",
                tenant_id=case.tenant_id, case_type_id=case.case_type_id)
        except statuses.StatusError as exc:
            logger.warning("order-case %s has no order but cannot be cancelled: %s", case.id, exc)
            continue
        await apply_status_change(session, case.id, "cancelled", SWEEP_ACTOR,
                                  "its order was never recorded", audit_action="cancelled")
        closed += 1
        logger.warning("order-case %s had no order after %d min — cancelled",
                       case.id, ORPHAN_GRACE_MINUTES)
    return closed
