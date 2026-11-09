"""Oct item 8f — a customer's subject-access export (GDPR Art. 15).

Pinned: everything the tenant holds about one person is found from what the
tenant knows them by — portal account, rep-desk profile (merges followed), their
cases (portal, links, orders), messages, documents, calls, conversations, SMS,
consent — and NOTHING from another tenant; internal notes and platform
internals are withheld unless an operator opts in; a relation never names the
other person; the request is gated, recorded and audited with counts only; and
the customer's own portal export is keyed to their account, not to what they type.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseMessageModel,
    CaseTypeModel,
    ChannelConversationModel,
    ChannelEndpointModel,
    ChannelMessageModel,
    CheckoutOrderModel,
    ConsentRecordModel,
    CustomerInteractionModel,
    CustomerRelationModel,
    DocumentModel,
    GDPRRequestModel,
    PortalCustomerModel,
    SecurityEventModel,
    SmsMessageModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.engage import identity
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

ADMIN = _headers("privacy-admin", ["admin"])
EMAIL, PHONE = "pat@x.test", "+447700900111"
URL = "/api/v1/enterprise/gdpr/customer-export"


@pytest.fixture(autouse=True)
def _customer_accounts_on():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("customer_accounts")
    _ENABLED_VERSIONS["customer_accounts"] = "v1.0.1"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("customer_accounts", None)
    else:
        _ENABLED_VERSIONS["customer_accounts"] = prior


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _case(db, t, ct, **kw):
    c = CaseInstanceModel(id=uuid.uuid4(), case_type_id=ct.id, case_type_version="1",
                          status="open", priority="medium", tenant_id=t.id, created_by="x", **kw)
    db.add(c)
    await db.flush()
    return c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default",
                        settings={"portal": {"enabled": True}})
        o = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([t, o])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="mal", tenant_id="other"))
        ct = CaseTypeModel(id=uuid.uuid4(), name="Claims", version="1", tenant_id=t.id,
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={})
        db.add(ct)
        await db.flush()
        pc = PortalCustomerModel(id=uuid.uuid4(), tenant_id=t.id, primary_email=EMAIL,
                                 display_name="Pat Customer", phone=PHONE, otp_code="h4sh")
        db.add(pc)
        # The rep desk: a profile known by the PHONE only, and a merged-away twin.
        prof = await identity.create_profile(db, tenant_uuid=t.id, display_name="Pat C")
        prof.notes = "rep's private view"
        await identity.add_identifier(db, tenant_uuid=t.id, customer_id=prof.id,
                                      kind="phone", value=PHONE)
        other_person = await identity.create_profile(db, tenant_uuid=t.id, display_name="Sam Else")
        db.add(CustomerRelationModel(id=uuid.uuid4(), tenant_id=t.id, from_customer_id=prof.id,
                                     to_customer_id=other_person.id, relation="household"))
        db.add(CustomerInteractionModel(id=uuid.uuid4(), tenant_id=t.id, customer_id=prof.id,
                                        channel="voice", ani=PHONE, status="wrapped",
                                        wrap_up_notes="angry caller", agent_user_id="rep7"))
        portal_case = await _case(db, t, ct, portal_submitter_email=EMAIL,
                                  data={"claim": "hail", "_engine": "x", "password_hint": "p"})
        linked = await _case(db, t, ct, data={"n": 2})
        await identity.link(db, tenant_uuid=t.id, customer_id=prof.id, object_kind="case",
                            object_id=linked.id)
        order_case = await _case(db, t, ct, data={"order": 1})
        db.add(CheckoutOrderModel(id=uuid.uuid4(), tenant_id="default", case_id=order_case.id,
                                  tracking_token="tt1", status="paid", currency="GBP",
                                  total_cents=100, customer={"email": EMAIL}, shipping={},
                                  order_meta={}, source="api"))
        stranger = await _case(db, t, ct, portal_submitter_email="someone@else.test",
                               data={"not": "pat"})
        db.add_all([
            CaseMessageModel(id=uuid.uuid4(), case_id=portal_case.id, author="staff",
                             body="We received your claim", portal_visible=True),
            CaseMessageModel(id=uuid.uuid4(), case_id=portal_case.id, author="staff",
                             body="internal: neighbour says otherwise", portal_visible=False),
            DocumentModel(id=uuid.uuid4(), case_id=portal_case.id, filename="photo.jpg",
                          content_type="image/jpeg", tenant_id=str(t.id), portal_visible=True),
            SmsMessageModel(id=uuid.uuid4(), tenant_id="default", case_id=linked.id, step_id="s",
                            provider="twilio", to_number=PHONE, body="Your claim moved",
                            status="sent"),
            ConsentRecordModel(id=uuid.uuid4(), subject_ref=EMAIL, subject_type="email",
                               purpose="marketing", action="granted", tenant_id="default"),
        ])
        ep = ChannelEndpointModel(id=uuid.uuid4(), tenant_id="default", channel="whatsapp",
                                  provider="twilio", address="+15550000")
        db.add(ep)
        await db.flush()
        conv = ChannelConversationModel(id=uuid.uuid4(), tenant_id="default", endpoint_id=ep.id,
                                        peer_address=f"whatsapp:{PHONE}", status="open")
        db.add(conv)
        await db.flush()
        db.add(ChannelMessageModel(id=uuid.uuid4(), tenant_id="default", conversation_id=conv.id,
                                   channel="whatsapp", direction="inbound",
                                   peer_address=f"whatsapp:{PHONE}", body="hello", status="received",
                                   raw_payload={"secret": "provider blob"}))
        # The SAME person in ANOTHER tenant — must never appear.
        oct_ = CaseTypeModel(id=uuid.uuid4(), name="Other claims", version="1", tenant_id=o.id,
                             lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={})
        db.add(oct_)
        await db.flush()
        db.add(PortalCustomerModel(id=uuid.uuid4(), tenant_id=o.id, primary_email=EMAIL,
                                   display_name="Pat At Other"))
        foreign = await _case(db, o, oct_, portal_submitter_email=EMAIL, data={"other": True})
        await db.commit()
    yield {"t": t, "pc": pc, "prof": prof, "portal_case": portal_case, "linked": linked,
           "order_case": order_case, "stranger": stranger, "foreign": foreign,
           "other_person": other_person}
    tenancy.clear_cache()


async def _export(api, h=ADMIN, **body):
    body.setdefault("reason", "subject access request #1")
    return await api.post(URL, headers=h, json=body)


async def test_everything_is_found_from_one_email(api, world):
    r = await _export(api, emails=[" PAT@x.test "])
    assert r.status_code == 200, r.text
    d = r.json()
    ids = {c["id"] for c in d["cases"]}
    assert ids == {str(world[k].id) for k in ("portal_case", "linked", "order_case")}
    assert str(world["stranger"].id) not in ids
    assert [p["id"] for p in d["profiles"]] == [str(world["prof"].id)]     # via the phone
    assert d["portal_accounts"][0]["primary_email"] == EMAIL
    assert len(d["orders"]) == 1 and len(d["sms"]) == 1 and len(d["consent"]) == 1
    assert len(d["conversations"]) == 1 and d["channel_messages"][0]["body"] == "hello"
    assert d["documents"][0]["filename"] == "photo.jpg"
    assert [m["body"] for m in d["case_messages"]] == ["We received your claim"]
    assert d["internal_notes_withheld"] == 1
    assert d["not_included"]


async def test_nothing_from_another_tenant_ever(api, world):
    blob = (await _export(api, emails=[EMAIL])).text
    assert "Pat At Other" not in blob and str(world["foreign"].id) not in blob
    r = await _export(api, h=_headers("mal", ["admin"]), emails=[EMAIL])
    d = r.json()
    assert [c["id"] for c in d["cases"]] == [str(world["foreign"].id)]
    assert "Pat Customer" not in r.text and "angry caller" not in r.text


async def test_internals_and_secrets_are_withheld(api, world):
    d = (await _export(api, emails=[EMAIL])).json()
    portal = next(c for c in d["cases"] if c["id"] == str(world["portal_case"].id))
    assert portal["data"] == {"claim": "hail"}
    blob = json.dumps(d)
    for leak in ("h4sh", "otp_code", "provider blob", "raw_payload", "angry caller",
                 "rep's private view", "rep7", "neighbour", "Sam Else"):
        assert leak not in blob, leak
    assert d["relations"] == [{"relation": "household", "may_act_for": False,
                               "direction": "from you", "created_at": d["relations"][0]["created_at"]}]


async def test_an_operator_may_include_internal_notes(api, world):
    d = (await _export(api, emails=[EMAIL], include_internal=True)).json()
    blob = json.dumps(d)
    assert "neighbour says otherwise" in blob and "angry caller" in blob
    assert d["internal_notes_withheld"] == 0


async def test_it_is_gated_recorded_and_audited_with_counts_only(api, world):
    for roles in (["viewer"], ["manager"], ["case_worker"]):
        assert (await _export(api, h=_headers("x", roles), emails=[EMAIL])).status_code == 403
    assert (await _export(api, emails=[])).status_code == 422
    assert (await api.post(URL, headers=ADMIN, json={"emails": [EMAIL]})).status_code == 422
    await _export(api, emails=[EMAIL])
    async with TestSessionFactory() as db:
        req = (await db.execute(select(GDPRRequestModel))).scalars().all()
        ev = (await db.execute(select(SecurityEventModel).where(
            SecurityEventModel.event_type == "gdpr.customer_exported"))).scalars().all()
    assert len(req) == 1 and req[0].status == "completed"
    assert len(ev) == 1 and ev[0].details["counts"]["cases"] == 3
    assert "hail" not in json.dumps(ev[0].details)


async def test_a_summary_answers_whether_and_records_nothing(api, world):
    r = await _export(api, emails=[EMAIL], summary=True)
    assert r.json()["exists"] is True and r.json()["counts"]["cases"] == 3
    assert "cases" not in {k for k, v in r.json().items() if isinstance(v, list)}
    async with TestSessionFactory() as db:
        assert not (await db.execute(select(GDPRRequestModel))).scalars().all()


async def test_the_customer_downloads_their_own_and_only_their_own(api, world):
    from portal_service.api.routers.portal_customers import _mint_customer_token
    token = _mint_customer_token(str(world["pc"].id), str(world["t"].id), "default")
    r = await api.get("/api/v1/portal/default/account/export",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert str(world["portal_case"].id) in {c["id"] for c in d["cases"]}
    assert "neighbour" not in r.text and "angry caller" not in r.text
    assert "Pat At Other" not in r.text
    # A token for this portal is not a key to another tenant's portal.
    assert (await api.get("/api/v1/portal/other/account/export",
                          headers={"Authorization": f"Bearer {token}"})).status_code in (401, 403, 404)
    assert (await api.get("/api/v1/portal/default/account/export")).status_code in (401, 403)


async def test_an_erased_customer_exports_as_erased(api, world):
    from case_service.engage import erasure
    async with TestSessionFactory() as db:
        await erasure.forget(db, tenant_uuid=world["t"].id, phones=[PHONE], reason="asked")
        await db.commit()
    d = (await _export(api, customer_ids=[str(world["prof"].id)])).json()
    assert d["profiles"][0]["status"] == "anonymized" and d["identifiers"] == []
