"""HxCheckout Order case-type template.

HxCheckout ships a built-in **Order** case type — the fulfilment workflow every
order runs through. The model is one GLOBAL default template (tenant_id=NULL)
that a tenant can later customise into its own copy via the Case Designer. Order
creation resolves the case type per tenant: it prefers a tenant-owned "Order"
type if one exists, otherwise falls back to the global default (seeding it lazily
on first use — "installed automatically on first use").

The template mirrors the default stages/steps in docs/Future/HxCheckout.md. It is
a normal case type and fully customisable in the Case Designer after seeding.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db import repository as repo
from case_service.db.models import CaseTypeModel

ORDER_TYPE_NAME = "Order"
ORDER_TYPE_VERSION = "1.0.0"

# Default fulfilment workflow. step_type is freeform; "auto" steps are advanced
# programmatically (e.g. Await Payment completes on the Stripe webhook), the rest
# are manual staff steps. Matches the doc's Order Case Type Template.
ORDER_DEFINITION: dict = {
    "stages": [
        {"id": "payment", "name": "Payment", "order": 1, "steps": [
            {"id": "await_payment",     "name": "Await Payment",     "step_type": "auto",   "required": True},
            {"id": "payment_confirmed", "name": "Payment Confirmed", "step_type": "auto",   "required": True},
        ]},
        {"id": "fulfilment", "name": "Fulfilment", "order": 2, "steps": [
            {"id": "assign_warehouse", "name": "Assign to Warehouse", "step_type": "manual", "required": True},
            {"id": "pick_pack",        "name": "Pick & Pack",         "step_type": "manual", "required": True},
            {"id": "dispatch",         "name": "Dispatch",            "step_type": "manual", "required": True},
        ]},
        {"id": "delivery", "name": "Delivery", "order": 3, "steps": [
            {"id": "in_transit", "name": "In Transit", "step_type": "auto",   "required": True},
            {"id": "delivered",  "name": "Delivered",  "step_type": "auto",   "required": True},
        ]},
        {"id": "post_delivery", "name": "Post-Delivery", "order": 4, "steps": [
            {"id": "review_window", "name": "7-Day Review Window", "step_type": "manual", "required": False},
            {"id": "closed",        "name": "Closed",              "step_type": "manual", "required": False},
        ]},
    ],
}


# Return sub-case: linked to the parent Order case when a customer requests a return.
RETURN_TYPE_NAME = "Order Return"
RETURN_TYPE_VERSION = "1.0.0"
RETURN_DEFINITION: dict = {
    "stages": [
        {"id": "requested", "name": "Return Requested", "order": 1, "steps": [
            {"id": "review_request", "name": "Review Request", "step_type": "manual", "required": True}]},
        {"id": "approved", "name": "Return Approved", "order": 2, "steps": [
            {"id": "issue_label", "name": "Issue Return Label", "step_type": "manual", "required": True}]},
        {"id": "received", "name": "Item Received", "order": 3, "steps": [
            {"id": "inspect", "name": "Inspect Item", "step_type": "manual", "required": True}]},
        {"id": "refunded", "name": "Refund Issued", "order": 4, "steps": [
            {"id": "refund", "name": "Process Refund", "step_type": "manual", "required": True}]},
    ],
}

# Complaint sub-case: uses the existing assignment + escalation engine.
COMPLAINT_TYPE_NAME = "Order Complaint"
COMPLAINT_TYPE_VERSION = "1.0.0"
COMPLAINT_DEFINITION: dict = {
    "stages": [
        {"id": "raised", "name": "Complaint Raised", "order": 1, "steps": [
            {"id": "triage", "name": "Triage", "step_type": "manual", "required": True}]},
        {"id": "investigating", "name": "Investigating", "order": 2, "steps": [
            {"id": "investigate", "name": "Investigate", "step_type": "manual", "required": True}]},
        {"id": "resolved", "name": "Resolved", "order": 3, "steps": [
            {"id": "resolve", "name": "Resolve & Close", "step_type": "manual", "required": True}]},
    ],
}


def tenant_uuid(tenant: str | None) -> uuid.UUID | None:
    """Map a free-form checkout tenant id to a case_types.tenant_id UUID, or None.

    Non-UUID tenants (e.g. "default", tenant-less single-tenant installs) map to
    NULL — the same rule create_case uses to avoid crashing the UUID column."""
    if not tenant:
        return None
    try:
        return uuid.UUID(str(tenant))
    except (ValueError, TypeError):
        return None


async def _get_global_order_type(session: AsyncSession) -> CaseTypeModel | None:
    return (await session.execute(
        select(CaseTypeModel).where(
            CaseTypeModel.name == ORDER_TYPE_NAME,
            CaseTypeModel.version == ORDER_TYPE_VERSION,
            CaseTypeModel.tenant_id.is_(None),
        ).limit(1)
    )).scalar_one_or_none()


async def _get_tenant_order_type(session: AsyncSession, tid: uuid.UUID) -> CaseTypeModel | None:
    """A tenant's own Order type (any version), if it customised the global one."""
    return (await session.execute(
        select(CaseTypeModel).where(
            CaseTypeModel.name == ORDER_TYPE_NAME,
            CaseTypeModel.tenant_id == tid,
        ).order_by(CaseTypeModel.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def resolve_order_case_type(session: AsyncSession, tenant: str | None) -> CaseTypeModel:
    """Resolve the Order case type for a tenant: prefer the tenant's own customised
    Order type, else the global default — seeding the global lazily and idempotently
    on first use. Concurrency-safe: a unique (name, version) collision from a racing
    first order is caught and the winning row refetched."""
    tid = tenant_uuid(tenant)
    if tid is not None:
        own = await _get_tenant_order_type(session, tid)
        if own is not None:
            return own

    existing = await _get_global_order_type(session)
    if existing is not None:
        return existing

    return await _seed_global_type(
        session, ORDER_TYPE_NAME, ORDER_TYPE_VERSION, ORDER_DEFINITION,
        description="HxCheckout order fulfilment workflow. Customisable per tenant in the Case Designer.",
        color="#db2777", tags=["hxcheckout", "order", "commerce"],
    )


async def _get_global_type(session: AsyncSession, name: str, version: str) -> CaseTypeModel | None:
    return (await session.execute(
        select(CaseTypeModel).where(
            CaseTypeModel.name == name,
            CaseTypeModel.version == version,
            CaseTypeModel.tenant_id.is_(None),
        ).limit(1)
    )).scalar_one_or_none()


async def _seed_global_type(
    session: AsyncSession, name: str, version: str, definition: dict,
    *, description: str = "", color: str = "#db2777", tags: list[str] | None = None,
) -> CaseTypeModel:
    """Idempotent get-or-create of a global (tenant_id=NULL) case type, race-safe on
    the (name, version) unique constraint."""
    existing = await _get_global_type(session, name, version)
    if existing is not None:
        return existing
    try:
        async with session.begin_nested():
            ct = await repo.create_case_type(session, data={
                "name": name, "version": version, "tenant_id": None,
                "default_priority": "medium", "definition_json": definition,
                "icon": "shopping-cart", "color": color,
                "description": description, "tags": tags or ["hxcheckout"],
            })
        return ct
    except IntegrityError:
        refetched = await _get_global_type(session, name, version)
        if refetched is None:
            raise
        return refetched


async def resolve_return_case_type(session: AsyncSession) -> CaseTypeModel:
    return await _seed_global_type(
        session, RETURN_TYPE_NAME, RETURN_TYPE_VERSION, RETURN_DEFINITION,
        description="HxCheckout return/refund sub-case linked to an Order case.",
        tags=["hxcheckout", "return"])


async def resolve_complaint_case_type(session: AsyncSession) -> CaseTypeModel:
    return await _seed_global_type(
        session, COMPLAINT_TYPE_NAME, COMPLAINT_TYPE_VERSION, COMPLAINT_DEFINITION,
        description="HxCheckout complaint sub-case linked to an Order case.",
        tags=["hxcheckout", "complaint"])
