"""Tests for P50 HxConnect — CRM (Salesforce) & Accounting (Xero)."""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import ConnectorRegistryModel, CrmSyncRecordModel, InvoiceRecordModel
from case_service.hxbridge.encryption import encrypt_credentials

from tests.conftest import client, session, deploy_case_type, create_case  # type: ignore[attr-defined]


async def _reg_sf(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="SF Test", connector_type="salesforce",
        config={"instance_url": "https://test.salesforce.com", "api_version": "59.0"},
        credentials=encrypt_credentials({"client_id": "cid", "client_secret": "csec", "refresh_token": "rtok"}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _reg_xero(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="Xero Test", connector_type="xero",
        config={},
        credentials=encrypt_credentials({"client_id": "xid", "client_secret": "xsec", "refresh_token": "xrtok", "xero_tenant_id": "xtenant"}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _crm_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="CRM Case", definition_json={
        "stages": [{"id": "s1", "name": "Onboard", "order": 1, "steps": [
            {"id": "crm_step", "name": "Sync to CRM", "step_type": "crm_sync", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


async def _inv_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="Invoice Case", definition_json={
        "stages": [{"id": "s1", "name": "Billing", "order": 1, "steps": [
            {"id": "inv_step", "name": "Generate Invoice", "step_type": "invoice_generate", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


# ── CRM list ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_crm_records_empty(client: AsyncClient):
    case = await _crm_case(client)
    r = await client.get(f"/api/v1/crm/cases/{case['id']}/records")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_crm_sync_no_connector_returns_400(client: AsyncClient):
    case = await _crm_case(client)
    r = await client.post(f"/api/v1/crm/cases/{case['id']}/sync",
                          json={"step_id": "crm_step", "first_name": "Alice", "email": "a@b.com"})
    assert r.status_code in (400, 502)


@pytest.mark.asyncio
async def test_crm_sync_unknown_case_returns_400(client: AsyncClient, session: AsyncSession):
    await _reg_sf(session); await session.commit()
    r = await client.post(f"/api/v1/crm/cases/{uuid.uuid4()}/sync",
                          json={"step_id": "crm_step", "first_name": "Alice"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_crm_record_created_and_retrievable(client: AsyncClient, session: AsyncSession):
    reg = await _reg_sf(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _crm_case(client)

    rec = CrmSyncRecordModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="crm_step",
        connector_id=reg.id, provider="salesforce",
        crm_object_type="Contact+Case", crm_record_id="0034x000ABCD",
        crm_record_url="https://test.salesforce.com/0034x000ABCD",
        status="synced", sync_data={"email": "a@b.com"},
    )
    session.add(rec); await session.commit()

    r = await client.get(f"/api/v1/crm/cases/{case['id']}/records")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "synced"
    assert rows[0]["crm_record_id"] == "0034x000ABCD"


@pytest.mark.asyncio
async def test_crm_pending_record_shows_error_on_failure(client: AsyncClient, session: AsyncSession):
    reg = await _reg_sf(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _crm_case(client)

    rec = CrmSyncRecordModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="crm_step",
        connector_id=reg.id, provider="salesforce",
        status="failed", sync_data={}, error="Token refresh failed",
    )
    session.add(rec); await session.commit()

    r = await client.get(f"/api/v1/crm/cases/{case['id']}/records")
    assert r.status_code == 200
    assert r.json()[0]["error"] == "Token refresh failed"


# ── Invoice list ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_invoices_empty(client: AsyncClient):
    case = await _inv_case(client)
    r = await client.get(f"/api/v1/invoices/cases/{case['id']}/records")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_invoice_no_connector_returns_400(client: AsyncClient):
    case = await _inv_case(client)
    r = await client.post(f"/api/v1/invoices/cases/{case['id']}/generate",
                          json={"step_id": "inv_step", "contact_name": "Alice", "amount_cents": 5000})
    assert r.status_code in (400, 502)


@pytest.mark.asyncio
async def test_invoice_unknown_case_returns_400(client: AsyncClient, session: AsyncSession):
    await _reg_xero(session); await session.commit()
    r = await client.post(f"/api/v1/invoices/cases/{uuid.uuid4()}/generate",
                          json={"step_id": "inv_step", "contact_name": "Alice", "amount_cents": 1000})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_invoice_record_created_and_retrievable(client: AsyncClient, session: AsyncSession):
    reg = await _reg_xero(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _inv_case(client)

    inv = InvoiceRecordModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="inv_step",
        connector_id=reg.id, provider="xero",
        invoice_id="INV-001-UUID", invoice_number="INV-001",
        invoice_url="https://go.xero.com/inv/001",
        amount_cents=15000, currency="gbp",
        contact_name="Acme Ltd", status="draft",
    )
    session.add(inv); await session.commit()

    r = await client.get(f"/api/v1/invoices/cases/{case['id']}/records")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "draft"
    assert rows[0]["invoice_number"] == "INV-001"
    assert rows[0]["amount_cents"] == 15000
    assert rows[0]["currency"] == "gbp"


@pytest.mark.asyncio
async def test_invoice_url_returned(client: AsyncClient, session: AsyncSession):
    reg = await _reg_xero(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _inv_case(client)

    inv = InvoiceRecordModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="inv_step",
        connector_id=reg.id, provider="xero",
        invoice_id="INV-002", invoice_number="INV-002",
        invoice_url="https://go.xero.com/inv/002",
        amount_cents=50000, currency="usd",
        contact_name="Customer X", status="draft",
    )
    session.add(inv); await session.commit()

    r = await client.get(f"/api/v1/invoices/cases/{case['id']}/records")
    assert r.json()[0]["invoice_url"] == "https://go.xero.com/inv/002"


@pytest.mark.asyncio
async def test_multiple_invoices_same_case(client: AsyncClient, session: AsyncSession):
    reg = await _reg_xero(session); await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _inv_case(client)

    for i in range(3):
        session.add(InvoiceRecordModel(
            tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id=f"inv_step_{i}",
            connector_id=reg.id, provider="xero",
            amount_cents=1000 * (i + 1), currency="usd",
            contact_name="Customer", status="draft",
        ))
    await session.commit()

    r = await client.get(f"/api/v1/invoices/cases/{case['id']}/records")
    assert r.status_code == 200
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_crm_connector_type_isolation(client: AsyncClient, session: AsyncSession):
    """Xero connector not found when requesting Salesforce sync."""
    await _reg_xero(session)   # only Xero registered, no Salesforce
    case = await _crm_case(client); await session.commit()
    r = await client.post(f"/api/v1/crm/cases/{case['id']}/sync",
                          json={"step_id": "crm_step", "first_name": "Alice"})
    assert r.status_code in (400, 502)
