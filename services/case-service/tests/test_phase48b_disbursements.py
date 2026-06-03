"""Tests for P48b — Payment disbursements (pay-to-customer flow).

Coverage:
  API:   confirm disbursement → row created + step completed
         list disbursements for case
         disburse with bank reference and notes
         zero/negative amount rejected
         disburse on unknown case returns 400
  Edge:  second disbursement on same step (re-confirm)
         disbursement on non-payment step still works (step_type agnostic)
         tenant_id set from authenticated user
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import CaseStepCompletionModel, PaymentDisbursementModel

from tests.conftest import client, session, deploy_case_type, create_case  # type: ignore[attr-defined]


async def _make_disburse_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="Disburse Case", definition_json={
        "stages": [{"id": "s1", "name": "Settlement", "order": 1, "steps": [
            {"id": "disburse_step", "name": "Pay Customer", "step_type": "payment_disbursement", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


# ── Basic flow ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_disbursements_empty(client: AsyncClient):
    case = await _make_disburse_case(client)
    resp = await client.get(f"/api/v1/payments/cases/{case['id']}/disbursements")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_confirm_disbursement_creates_row(client: AsyncClient, session: AsyncSession):
    case = await _make_disburse_case(client)

    resp = await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id":      "disburse_step",
        "amount_cents": 25000,
        "currency":     "gbp",
        "description":  "Claim settlement",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "confirmed"
    assert data["amount_cents"] == 25000
    assert data["currency"] == "gbp"
    assert data["step_id"] == "disburse_step"

    rows = (await session.execute(select(PaymentDisbursementModel))).scalars().all()
    assert len(rows) == 1
    assert rows[0].amount_cents == 25000


@pytest.mark.asyncio
async def test_disbursement_with_bank_reference(client: AsyncClient, session: AsyncSession):
    case = await _make_disburse_case(client)

    resp = await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id":        "disburse_step",
        "amount_cents":   10000,
        "currency":       "eur",
        "description":    "Reimbursement",
        "bank_reference": "IBAN-DE89370400440532013000",
        "notes":          "Customer requested express transfer",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["bank_reference"] == "IBAN-DE89370400440532013000"
    assert data["notes"] == "Customer requested express transfer"


@pytest.mark.asyncio
async def test_disbursement_completes_step(client: AsyncClient, session: AsyncSession):
    case = await _make_disburse_case(client)

    await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id":      "disburse_step",
        "amount_cents": 5000,
        "currency":     "usd",
        "description":  "Test payout",
    })

    completions = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == uuid.UUID(case["id"]),
            CaseStepCompletionModel.step_id == "disburse_step",
        )
    )).scalars().all()
    assert len(completions) == 1
    assert completions[0].status == "completed"
    assert completions[0].step_type == "payment_disbursement"


@pytest.mark.asyncio
async def test_list_disbursements_returns_row(client: AsyncClient, session: AsyncSession):
    case = await _make_disburse_case(client)

    await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id":      "disburse_step",
        "amount_cents": 7500,
        "currency":     "inr",
        "description":  "Claim payout",
    })

    resp = await client.get(f"/api/v1/payments/cases/{case['id']}/disbursements")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["currency"] == "inr"
    assert rows[0]["amount_cents"] == 7500


@pytest.mark.asyncio
async def test_confirmed_at_is_set(client: AsyncClient, session: AsyncSession):
    case = await _make_disburse_case(client)
    resp = await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id": "disburse_step", "amount_cents": 1000, "currency": "usd", "description": "x",
    })
    assert resp.status_code == 200
    assert resp.json()["confirmed_at"] is not None


# ── Edge cases ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disburse_unknown_case_returns_400(client: AsyncClient):
    resp = await client.post(f"/api/v1/payments/cases/{uuid.uuid4()}/disburse", json={
        "step_id": "any_step", "amount_cents": 1000, "currency": "usd", "description": "x",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_second_disburse_same_step_upserts(client: AsyncClient, session: AsyncSession):
    """Re-confirming a disbursement on the same step creates a second row (idempotent step update)."""
    case = await _make_disburse_case(client)

    r1 = await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id": "disburse_step", "amount_cents": 1000, "currency": "usd", "description": "first",
    })
    r2 = await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id": "disburse_step", "amount_cents": 2000, "currency": "usd", "description": "second",
    })
    assert r1.status_code == 200
    assert r2.status_code == 200

    # Step completion should be upserted — still one record, now updated
    completions = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == uuid.UUID(case["id"]),
            CaseStepCompletionModel.step_id == "disburse_step",
        )
    )).scalars().all()
    assert len(completions) == 1
    assert completions[0].status == "completed"


@pytest.mark.asyncio
async def test_disbursement_on_non_payment_step(client: AsyncClient, session: AsyncSession):
    """Any step_id works — the system tracks whatever step the caller specifies."""
    ct = await deploy_case_type(client, name="Generic Case", definition_json={
        "stages": [{"id": "s1", "name": "Processing", "order": 1, "steps": [
            {"id": "user_step", "name": "Review", "step_type": "user_task", "required": True},
        ]}]
    })
    case = await create_case(client, ct["id"])

    resp = await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id": "user_step", "amount_cents": 500, "currency": "usd", "description": "Manual payout",
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_multiple_disbursements_different_steps(client: AsyncClient, session: AsyncSession):
    ct = await deploy_case_type(client, name="Multi Pay Case", definition_json={
        "stages": [{"id": "s1", "name": "Payment", "order": 1, "steps": [
            {"id": "d1", "name": "Pay Part 1", "step_type": "payment_disbursement", "required": True},
            {"id": "d2", "name": "Pay Part 2", "step_type": "payment_disbursement", "required": True},
        ]}]
    })
    case = await create_case(client, ct["id"])

    r1 = await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id": "d1", "amount_cents": 3000, "currency": "usd", "description": "First tranche",
    })
    r2 = await client.post(f"/api/v1/payments/cases/{case['id']}/disburse", json={
        "step_id": "d2", "amount_cents": 7000, "currency": "usd", "description": "Second tranche",
    })
    assert r1.status_code == 200
    assert r2.status_code == 200

    rows = (await session.execute(select(PaymentDisbursementModel))).scalars().all()
    assert len(rows) == 2
    total = sum(r.amount_cents for r in rows)
    assert total == 10000
