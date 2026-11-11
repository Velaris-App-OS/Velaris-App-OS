"""case-service's part of the website's demo door — `/internal/v1/demo/*`
(2026-11-10): erase a website lead for good.

Called only by the website's registration service, straight to case-service on
host loopback (never through the gateway, which refuses `/internal`), with its
demo provisioning secret (`require_demo_caller`) — the one platform credential
the website holds.

It acts in ONE tenant only: `VELARIS_DEMO_LEADS_TENANT` (the operator's
"sales" tenant, made by the website's setup-sales-leads.py). Unset = this route
answers 404 and touches nothing (an install that never set it is unchanged).
Within that tenant it erases every case whose `email` field, or portal
submitter address, is the one given — permanently (compliance/erasure.py: audit
rows pruned with a sealed record, every case-linked row and stored file,
the case). A case under a legal hold is kept and reported.

Kept afterwards: a GDPR request record with the subject as a sha256
FINGERPRINT, never the address — proof the erasure happened, with nothing
personal in it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.config import get_settings
from velaris_shared.db.session import get_session
from velaris_shared.internal.auth import require_demo_caller

router = APIRouter(prefix="/internal/v1/demo", tags=["internal-demo"],
                   dependencies=[Depends(require_demo_caller)], include_in_schema=False)


class EraseLeadIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")


def fingerprint(email: str) -> str:
    return "sha256:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


@router.post("/erase-lead")
async def erase_lead(body: EraseLeadIn, session: AsyncSession = Depends(get_session)):
    from velaris_shared.db.models import CaseInstanceModel
    from velaris_shared.tenancy.identity import resolve_tenant
    from case_service.compliance import erasure
    from case_service.enterprise import gdpr

    slug = (get_settings().demo_leads_tenant or "").strip()
    if not slug:
        raise HTTPException(404, "Not Found")
    hit = await resolve_tenant(session, slug)
    if not hit:
        raise HTTPException(404, "Not Found")
    tid = hit[0]
    want = body.email.strip().lower()
    ids = []
    for c in (await session.execute(select(CaseInstanceModel).where(
            CaseInstanceModel.tenant_id == tid))).scalars():
        data = c.data if isinstance(c.data, dict) else {}
        emails = {str(data.get("email") or "").strip().lower(),
                  str(getattr(c, "portal_submitter_email", "") or "").strip().lower()}
        if want in emails:
            ids.append(c.id)
    out = await erasure.erase_cases(session, ids, actor="system:website-erasure", reason="erasure")
    req_id = await gdpr.create_request(session, subject_id=fingerprint(want), request_type="delete",
                                       requested_by="website", reason="website account erased or consent withdrawn",
                                       tenant_id=tid)
    if not out["held"]:
        await gdpr.complete_request(session, req_id)
    await session.commit()
    return {"erased": len(out["erased"]), "held": len(out["held"]), "rows": out["rows"],
            "request_id": str(req_id)}


async def _erase_data(session, tid):
    from case_service.compliance.erasure import erase_tenant_data
    await erase_tenant_data(session, tid, actor="system:website-erasure")


async def _held(session, tid):
    from case_service.legal_hold.service import tenant_active_count
    return await tenant_active_count(session, tid)


@router.delete("/tenants/{tenant_id}")
async def delete_trial_tenant(tenant_id: str, session: AsyncSession = Depends(get_session)):
    """"Delete everything now" on the website: a build's trial tenant only
    (kind `trial`), with its cases and data; any other tenant answers 404."""
    from velaris_shared.tenancy import demo
    try:
        return await demo.delete_trial(session, tenant_id, _held, _erase_data)
    except demo.DemoError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/tenants/{tenant_id}/erase")
async def erase_demo_tenant(tenant_id: str, session: AsyncSession = Depends(get_session)):
    """A website account erased: its demo tenant (any kind the website made),
    with its cases and data. Any tenant without the demo mark answers 404."""
    from velaris_shared.tenancy import demo
    try:
        return await demo.erase_demo(session, tenant_id, _held, _erase_data)
    except demo.DemoError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
