"""F-01 — the AI decision ledger API.

The auditor surface. Four things it must be able to answer, and each is a route:

* *What decided this, and on what basis?* — `GET /records`
* *Which models are actually in use, and did any run externally?* — `GET /models`
* *Was a human involved, and what did they do?* — `POST /records/{id}/verdict`
  writes it; `GET /records` reads it back.
* *Can you prove none of it has been edited?* — `GET /chain`

Two access decisions worth stating:

* **Reading the ledger is not reading cases.** `ai_ledger.records.read` is
  separate from every case action, and the ledger stores hashes rather than
  content so that separation is real rather than promised.
* **Recording a verdict is case work.** The person who accepts or rejects an AI
  proposal is the case worker looking at it, so `verdicts.write` is held by case
  workers and NOT implied by the auditor's read — an auditor who could also
  record verdicts could manufacture the oversight they are auditing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import AIDecisionRecordModel
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.core.timestamps import iso_utc
from case_service.ledger import chain, evidence, records
from case_service.tenancy.identity import resolve_tenant
from velaris_shared.db.read_routing import get_read_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ai-ledger", tags=["ai-ledger"])

#: Building a pack walks every record in the period and recomputes the whole
#: chain. It is the one expensive call here.
_pack_rl = rate_limit(max_calls=10, window_seconds=60, name="ai-ledger-pack")
_verify_rl = rate_limit(max_calls=20, window_seconds=60, name="ai-ledger-verify")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerdictIn(_Strict):
    verdict: str = Field(min_length=1, max_length=20)   # accepted|edited|rejected
    reason: str = Field(default="", max_length=4000)
    #: What the human changed. Hashed; the text itself is kept only when the
    #: deployment has opted into raw retention, exactly as prompts are.
    diff: str | None = Field(default=None, max_length=100_000)


async def _tenant_uuid(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    token = user.tenant_id or (user.attributes or {}).get("tenant_id")
    resolved = await resolve_tenant(session, token)
    if resolved is None:
        raise HTTPException(403, "VEL-LEDGER-TENANT-UNRESOLVED-403")
    return resolved[0]


async def _load(
    session: AsyncSession, record_id: uuid.UUID, tenant: uuid.UUID,
) -> AIDecisionRecordModel:
    row = await session.get(AIDecisionRecordModel, record_id)
    # 404 and not 403 for another tenant's record: a 403 confirms the id exists.
    if row is None or row.tenant_id != tenant:
        raise HTTPException(404, "VEL-LEDGER-404: no such decision record")
    return row


def _record_json(r: AIDecisionRecordModel, verdict: Any | None) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "recorded_at": iso_utc(r.recorded_at),
        "purpose": r.purpose,
        "case_id": str(r.case_id) if r.case_id else None,
        "step_id": r.step_id,
        "agent": {"id": str(r.agent_id) if r.agent_id else None,
                  "slug": r.agent_slug, "kind": r.agent_kind},
        "model": {"id": r.model_id, "provider_class": r.provider_class},
        "input_sha256": r.input_sha256,
        "input_data_classes": r.input_data_classes,
        "output_sha256": r.output_sha256,
        # Whether the raw text was kept is reported; the text itself is never
        # returned by this API. Reading a stored prompt is case-content access
        # and does not belong behind a governance action.
        "raw_retained": bool(r.prompt_sealed or r.output_sealed),
        "confidence": float(r.confidence) if r.confidence is not None else None,
        "latency_ms": float(r.latency_ms) if r.latency_ms is not None else None,
        "policy_decision_id": (str(r.arbiter_decision_id)
                               if r.arbiter_decision_id else None),
        "sealed_in_batch": r.chain_sequence,
        "human_review": (
            None if verdict is None else {
                "verdict": verdict.verdict,
                "by": verdict.decided_by,
                "at": iso_utc(verdict.decided_at),
                "reason": verdict.reason,
                "diff_sha256": verdict.diff_sha256,
            }
        ),
    }


@router.get("/records")
async def list_records(
    case_id: uuid.UUID | None = Query(None),
    agent_id: uuid.UUID | None = Query(None),
    purpose: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("ai_ledger.records.read")),
) -> dict[str, Any]:
    tenant = await _tenant_uuid(session, user)
    rows = await records.list_records(
        session, tenant_id=tenant, case_id=case_id, agent_id=agent_id,
        purpose=purpose, limit=limit)
    verdicts = await records.latest_verdicts(session, [r.id for r in rows])
    return {
        "records": [_record_json(r, verdicts.get(str(r.id))) for r in rows],
        "count": len(rows),
    }


@router.get("/records/{record_id}")
async def get_record(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("ai_ledger.records.read")),
) -> dict[str, Any]:
    tenant = await _tenant_uuid(session, user)
    row = await _load(session, record_id, tenant)
    verdicts = await records.latest_verdicts(session, [row.id])
    body = _record_json(row, verdicts.get(str(row.id)))
    # The FULL verdict history, not just the newest — "accepted, then reversed
    # three days later" is the sequence this table exists to keep.
    from sqlalchemy import select
    from velaris_shared.db.models import AIDecisionVerdictModel
    history = (await session.execute(
        select(AIDecisionVerdictModel)
        .where(AIDecisionVerdictModel.record_id == row.id)
        .order_by(AIDecisionVerdictModel.decided_at)
    )).scalars().all()
    body["review_history"] = [
        {"verdict": v.verdict, "by": v.decided_by, "at": iso_utc(v.decided_at),
         "reason": v.reason, "diff_sha256": v.diff_sha256}
        for v in history
    ]
    return body


@router.post("/records/{record_id}/verdict", status_code=201)
async def add_verdict(
    record_id: uuid.UUID,
    payload: VerdictIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("ai_ledger.verdicts.write")),
) -> dict[str, Any]:
    """Record what a human decided. Appends; never replaces."""
    tenant = await _tenant_uuid(session, user)
    row = await _load(session, record_id, tenant)
    try:
        v = await records.record_verdict(
            session, record_id=row.id, verdict=payload.verdict,
            decided_by=str(user.user_id), reason=payload.reason,
            diff_text=payload.diff)
    except records.LedgerError as exc:
        raise HTTPException(422, f"VEL-LEDGER-INVALID-422: {exc}") from exc
    await session.commit()
    return {"id": str(v.id), "record_id": str(row.id), "verdict": v.verdict,
            "at": iso_utc(v.decided_at)}


@router.get("/models")
async def model_inventory(
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("ai_ledger.records.read")),
) -> dict[str, Any]:
    """Which models decided anything here — from what ran, not from config."""
    tenant = await _tenant_uuid(session, user)
    inventory = await records.model_inventory(session, tenant_id=tenant)
    return {
        "models": [
            {**m, "first_seen": iso_utc(m["first_seen"]),
             "last_seen": iso_utc(m["last_seen"])}
            for m in inventory
        ],
        "overrides": await records.override_rates(session, tenant_id=tenant),
    }


@router.get("/chain", dependencies=[Depends(_verify_rl)])
async def verify(
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("ai_ledger.chain.verify")),
) -> dict[str, Any]:
    """Recompute every link. This is the product claim executed as code."""
    ok, note = await chain.verify_chain(session)
    return {"verified": ok, "note": note}


@router.post("/seal", status_code=200)
async def seal(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("ai_ledger.chain.verify")),
) -> dict[str, Any]:
    """Seal whatever is ready now, rather than waiting for the loop.

    Gated on `chain.verify` rather than a write action deliberately: sealing
    adds evidence and removes nothing, it is idempotent, and an auditor who has
    just been told "3 decisions are not yet sealed" should be able to resolve
    that themselves instead of filing a request.
    """
    return {"sealed_batches": await chain.seal_once(session)}


@router.get("/evidence-pack", dependencies=[Depends(_pack_rl)])
async def evidence_pack(
    case_id: uuid.UUID | None = Query(None),
    days: int = Query(30, ge=1, le=3650),
    # Stays on the main: the export's HxArbiter decision is recorded through this
    # session (found in R5d 11b — read-only refused that write).
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("ai_ledger.evidence.export")),
) -> dict[str, Any]:
    """The Article 22 dossier — for one case, or for a period.

    Passes through the HxArbiter `export.evidence` enforcement point as well as
    the HxGuard action: an auditor download is itself a logged, policy-checked
    decision. In `shadow` — where the `export` namespace ships — this changes
    nothing and records the evidence a promotion is later argued from.
    """
    tenant = await _tenant_uuid(session, user)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    from case_service.hxarbiter import pep as _arb_pep
    allowed = await _arb_pep.check(
        session,
        tenant_id=str(tenant),
        action=_arb_pep.ACTION_EXPORT_EVIDENCE,
        subject_id=str(user.user_id),
        resource_kind="evidence_pack",
        resource_id=str(case_id) if case_id else None,
        context={"purpose": "ai_governance"},
        legacy_allowed=True,        # the HxGuard action above is the control
        claimed_by="ai_ledger.evidence",
    )
    if not allowed:
        raise HTTPException(
            403, "VEL-LEDGER-EXPORT-DENIED-403: policy refuses this export")

    built = await evidence.build_pack(
        session, tenant_id=tenant, period_start=start, period_end=end,
        case_id=case_id, generated_by=str(user.user_id))
    return {"summary": built["summary"], "pack": built["pack"]}
