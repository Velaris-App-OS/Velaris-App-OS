"""Permanent erasure of a website lead (2026-11-10) — `POST /internal/v1/demo/erase-lead`.

Leads are cases in the operator's "sales" tenant (VELARIS_DEMO_LEADS_TENANT). When
a visitor withdraws consent or deletes their website account, the website asks
case-service, through its demo door, to erase every case holding that address
— for good: audit rows pruned with a sealed record (the chain still verifies),
every case-linked row and stored file, the case. A held case stays. Only a
fingerprint of the address is kept.

Cases are made through the API by a sales-tenant user, as the website does;
the tenant, the user's directory row and its access group are the fixture.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from velaris_shared.config import get_settings
from velaris_shared.db.models import (
    CaseAuditLogModel, CaseInstanceModel, DocumentModel, GDPRRequestModel, TenantModel,
    UserDirectoryModel,
)
from velaris_shared.internal.auth import DEMO_HEADER
from case_service.compliance import audit_chain as ac
from case_service.main import app
from tests.conftest import TestSessionFactory, _headers, deploy_case_type, grant_case_create

SECRET = "d" * 40
PATH = "/internal/v1/demo/erase-lead"


@pytest.fixture
def leads_on():
    s = get_settings()
    s.demo_provision_secret, s.demo_leads_tenant = SECRET, "sales"
    yield
    s.demo_provision_secret, s.demo_leads_tenant = "", ""


@pytest.fixture
async def raw():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def world(client, leads_on):
    """A sales tenant + another tenant (own names per test: the test database
    is shared); one intake user in each, able to create cases."""
    n = uuid.uuid4().hex[:6]
    sales_slug, acme_slug = f"sales-{n}", f"acme-{n}"
    get_settings().demo_leads_tenant = sales_slug
    async with TestSessionFactory() as db:
        sales = TenantModel(id=uuid.uuid4(), slug=sales_slug, name="Sales")
        acme = TenantModel(id=uuid.uuid4(), slug=acme_slug, name="Acme")
        db.add_all([sales, acme])
        db.add_all([UserDirectoryModel(id=uuid.uuid4(), user_id=f"leads-{n}", tenant_id=sales_slug),
                    UserDirectoryModel(id=uuid.uuid4(), user_id=f"acme-{n}", tenant_id=acme_slug)])
        await db.commit()
    await grant_case_create(f"leads-{n}", tenant=sales_slug)
    await grant_case_create(f"acme-{n}", tenant=acme_slug)
    ct_sales = await deploy_case_type(client, name=f"Website Lead {n}", tenant_id=str(sales.id))
    ct_acme = await deploy_case_type(client, name=f"Acme Intake {n}", tenant_id=str(acme.id))
    return {"sales": sales, "acme": acme, "ct_sales": ct_sales["id"], "ct_acme": ct_acme["id"],
            "intake": f"leads-{n}", "acme_intake": f"acme-{n}", "slug": sales_slug}


async def _lead(client, world, email: str, *, in_acme: bool = False) -> str:
    who, ct = (world["acme_intake"], world["ct_acme"]) if in_acme else (world["intake"], world["ct_sales"])
    r = await client.post("/api/v1/cases", json={"case_type_id": ct, "data": {"email": email, "company": "X"}},
                          headers=_headers(who, ["viewer"]))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seal() -> None:
    """Seal the chain; the app's background sealer writes the same SQLite file,
    so a busy moment is retried (Postgres in production has no such lock)."""
    import asyncio
    for i in range(20):
        try:
            async with TestSessionFactory() as db:
                await ac.seal_new_entries(db)
                await db.commit()
            return
        except Exception as exc:                 # noqa: BLE001
            if "locked" not in str(exc) or i == 19:
                raise
            await asyncio.sleep(0.1)


async def _exists(case_id: str) -> bool:
    async with TestSessionFactory() as db:
        return await db.get(CaseInstanceModel, uuid.UUID(case_id)) is not None


async def test_unset_tenant_means_the_route_does_nothing(raw, client, world):
    get_settings().demo_leads_tenant = ""
    r = await raw.post(PATH, json={"email": "a@x.io"}, headers={DEMO_HEADER: SECRET})
    assert r.status_code == 404


async def test_only_the_websites_demo_secret_opens_it(raw, client, world):
    assert (await raw.post(PATH, json={"email": "a@x.io"})).status_code == 403
    assert (await raw.post(PATH, json={"email": "a@x.io"}, headers={DEMO_HEADER: "x" * 40})).status_code == 403
    # never through a proxy (the gateway's headers)
    r = await raw.post(PATH, json={"email": "a@x.io"}, headers={DEMO_HEADER: SECRET, "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 404


async def test_erases_that_persons_leads_and_nothing_else(raw, client, world):
    mine1 = await _lead(client, world, "Ann@Corp.io")
    mine2 = await _lead(client, world, "ann@corp.io")
    theirs = await _lead(client, world, "bob@corp.io")
    other_tenant = await _lead(client, world, "ann@corp.io", in_acme=True)

    r = await raw.post(PATH, json={"email": "ann@corp.io"}, headers={DEMO_HEADER: SECRET})
    assert r.status_code == 200, r.text
    assert r.json()["erased"] == 2 and r.json()["held"] == 0
    assert not await _exists(mine1) and not await _exists(mine2)
    assert await _exists(theirs), "another person's lead is untouched"
    assert await _exists(other_tenant), "the same address in another tenant is untouched"


async def test_audit_rows_go_and_the_chain_still_verifies(raw, client, world):
    case_id = await _lead(client, world, "ann@corp.io")
    keep = await _lead(client, world, "bob@corp.io")
    await _seal()
    async with TestSessionFactory() as db:
        n = (await db.execute(select(func.count()).select_from(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == uuid.UUID(case_id)))).scalar_one()
    assert n >= 1, "creating the lead wrote audit rows"
    assert (await raw.post(PATH, json={"email": "ann@corp.io"}, headers={DEMO_HEADER: SECRET})).status_code == 200
    async with TestSessionFactory() as db:
        left = (await db.execute(select(func.count()).select_from(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == uuid.UUID(case_id)))).scalar_one()
        assert left == 0
    await _seal()
    async with TestSessionFactory() as db:
        v = await ac.verify_chain(db)
    assert v["verified"], v
    assert await _exists(keep)


async def test_linked_rows_and_their_stored_files_go_too(raw, client, world, tmp_path, monkeypatch):
    case_id = await _lead(client, world, "ann@corp.io")
    deleted = []

    class FakeStore:
        async def delete(self, key):
            deleted.append(key)
    monkeypatch.setattr("velaris_shared.storage.factory.get_storage_backend", lambda: FakeStore())
    r = await client.post(f"/api/v1/cases/{case_id}/notes", json={"text": "called her"},
                          headers=_headers(world["intake"], ["viewer"]))
    async with TestSessionFactory() as db:   # a document as the document manager stores one
        db.add(DocumentModel(id=uuid.uuid4(), case_id=uuid.UUID(case_id), filename="cv.pdf", tenant_id=world["slug"]))
        await db.commit()
    out = (await raw.post(PATH, json={"email": "ann@corp.io"}, headers={DEMO_HEADER: SECRET})).json()
    assert out["erased"] == 1
    assert out["rows"].get("documents") == 1
    async with TestSessionFactory() as db:
        assert (await db.execute(select(func.count()).select_from(DocumentModel).where(
            DocumentModel.case_id == uuid.UUID(case_id)))).scalar_one() == 0


async def test_a_lead_under_a_legal_hold_is_kept(raw, client, world, monkeypatch):
    held = await _lead(client, world, "ann@corp.io")
    from case_service.legal_hold import service as lh

    async def only_held(session, col):
        return col != uuid.UUID(held)
    monkeypatch.setattr(lh, "not_held", only_held)
    out = (await raw.post(PATH, json={"email": "ann@corp.io"}, headers={DEMO_HEADER: SECRET})).json()
    assert out["erased"] == 0 and out["held"] == 1
    assert await _exists(held)


async def test_the_record_kept_has_no_personal_data(raw, client, world):
    await _lead(client, world, "ann@corp.io")
    await raw.post(PATH, json={"email": "ann@corp.io"}, headers={DEMO_HEADER: SECRET})
    async with TestSessionFactory() as db:
        rows = (await db.execute(select(GDPRRequestModel).where(
            GDPRRequestModel.tenant_id == world["sales"].id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].subject_id == "sha256:" + hashlib.sha256(b"ann@corp.io").hexdigest()
    assert "ann" not in (rows[0].subject_id + (rows[0].reason or "") + (rows[0].requested_by or ""))
    assert rows[0].request_type == "delete" and rows[0].status == "completed"


def test_every_table_with_a_case_reference_is_reached():
    """A table added later with a case_id is erased without anyone listing it."""
    from case_service.compliance import erasure
    from velaris_shared.db.models import Base
    reached = {t.name for t, _ in erasure.dependents()}
    for t in Base.metadata.sorted_tables:
        if t.name in erasure.SKIP:
            continue
        if any(c in t.c for c in erasure.CASE_COLUMNS):
            assert t.name in reached, t.name


# ── deleting a website-made tenant that HAS cases (found in the live run, 2026-11-10) ──
# A tenant with cases could not be deleted at all: case_instances (and case types,
# forms, rules, work queues …) reference `tenants` without ON DELETE CASCADE.

async def _seed_builtins() -> None:
    """What migration 033 seeds on a real install: the staff portal and the
    built-in `admin` access role (full access) the demo group holds."""
    from velaris_shared.db.models import AccessRoleModel, PortalModel
    from velaris_shared.tenancy import demo
    async with TestSessionFactory() as db:
        if await db.get(PortalModel, demo._STAFF_PORTAL) is None:
            db.add(PortalModel(id=demo._STAFF_PORTAL, name="Staff", portal_type="staff"))
        if await db.get(AccessRoleModel, uuid.UUID(demo._ADMIN_ROLE)) is None:
            db.add(AccessRoleModel(id=uuid.UUID(demo._ADMIN_ROLE), name="admin",
                                   privileges=[{"resource": "*", "case_type_id": "*", "actions": ["*"]}]))
        await db.commit()


async def _demo_with_a_case(client, kind: str) -> dict:
    from velaris_shared.tenancy import demo
    await _seed_builtins()
    n = uuid.uuid4().hex[:6]
    async with TestSessionFactory() as db:
        out = await demo.provision(db, request_key=f"key-{kind}-{n}", username=f"{kind}_{n}",
                                   email=f"{kind}{n}@x.invalid", password="Pw-123456789!",
                                   display_name=None, kind=kind)
        await db.commit()
    await grant_case_create(out["username"], tenant=out["tenant_slug"])
    ct = await deploy_case_type(client, name=f"App {n}", tenant_id=out["tenant_id"])
    from velaris_shared.auth.jwt_handler import create_dev_token
    st = get_settings()
    tok = create_dev_token(user_id=out["user_id"], username=out["username"], roles=["admin"],
                           secret=st.auth_secret, private_key=st.auth_rsa_private_key or "")
    r = await client.post("/api/v1/cases", json={"case_type_id": ct["id"], "data": {"x": 1}},
                          headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 201, r.text
    return {**out, "case_id": r.json()["id"], "case_type_id": ct["id"]}


async def _tenant_gone(tid: str) -> bool:
    async with TestSessionFactory() as db:
        return await db.get(TenantModel, uuid.UUID(tid)) is None


async def test_delete_everything_now_removes_a_trial_with_its_cases(raw, client, leads_on):
    t = await _demo_with_a_case(client, "trial")
    signup = await _demo_with_a_case(client, "free")
    r = await raw.delete(f"/internal/v1/demo/tenants/{t['tenant_id']}", headers={DEMO_HEADER: SECRET})
    assert r.status_code == 200, r.text
    assert await _tenant_gone(t["tenant_id"]) and not await _exists(t["case_id"])
    async with TestSessionFactory() as db:
        assert (await ac.verify_chain(db))["verified"]
    # trial-only: a sign-up's tenant and any ordinary tenant answer 404
    assert (await raw.delete(f"/internal/v1/demo/tenants/{signup['tenant_id']}",
                             headers={DEMO_HEADER: SECRET})).status_code == 404
    assert (await raw.delete("/internal/v1/demo/tenants/default", headers={DEMO_HEADER: SECRET})).status_code == 404
    assert (await raw.delete(f"/internal/v1/demo/tenants/{signup['tenant_id']}")).status_code == 403
    assert await _exists(signup["case_id"])


async def test_an_account_erasure_removes_its_signup_tenant_with_its_cases(raw, client, leads_on):
    s = await _demo_with_a_case(client, "work")
    r = await raw.post(f"/internal/v1/demo/tenants/{s['tenant_slug']}/erase", headers={DEMO_HEADER: SECRET})
    assert r.status_code == 200, r.text
    assert await _tenant_gone(s["tenant_id"]) and not await _exists(s["case_id"])
    async with TestSessionFactory() as db:
        db.add(TenantModel(id=uuid.uuid4(), slug=f"demo-look-{uuid.uuid4().hex[:4]}", name="Real",
                           settings={"demo": "no"}))
        await db.commit()
        real = (await db.execute(select(TenantModel).where(TenantModel.name == "Real"))).scalars().first()
    assert (await raw.post(f"/internal/v1/demo/tenants/{real.slug}/erase", headers={DEMO_HEADER: SECRET})).status_code == 404


async def test_the_hourly_expiry_deletes_an_expired_trial_with_cases(client, leads_on):
    from datetime import datetime, timedelta, timezone
    from case_service.tenancy import demo_expiry
    t = await _demo_with_a_case(client, "trial")
    async with TestSessionFactory() as db:
        row = await db.get(TenantModel, uuid.UUID(t["tenant_id"]))
        d = dict(row.settings["demo"]); d["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        row.settings = {**row.settings, "demo": d}
        await db.commit()
    async with TestSessionFactory() as db:
        out = await demo_expiry.run_once(db)
    assert out["deleted"] >= 1
    assert await _tenant_gone(t["tenant_id"]) and not await _exists(t["case_id"])


async def test_the_operators_permanent_delete_works_for_a_tenant_with_cases(client, leads_on):
    t = await _demo_with_a_case(client, "free")
    r = await client.delete(f"/api/v1/tenants/{t['tenant_id']}/permanent")
    assert r.status_code == 204, r.text
    assert await _tenant_gone(t["tenant_id"]) and not await _exists(t["case_id"])
    async with TestSessionFactory() as db:
        assert (await ac.verify_chain(db))["verified"]
