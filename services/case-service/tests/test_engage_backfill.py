"""Oct item 3 — profile backfill that PROPOSES. And the ladder it reads from.

The item's rules, pinned:

* run explicitly by an operator, and re-runnable;
* it never creates or merges on its own — the scan writes proposals only, and
  a person accepts each, re-checked at that moment;
* never manufacture a person from an unverified inbound number.

Plus three defects found in the resolution ladder while building it: rungs 3
and 4 had no tenant clause (another tenant's customer named a profile here),
and rung 2 never matched on PostgreSQL.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    ChannelConversationModel,
    ChannelEndpointModel,
    CustomerBackfillProposalModel,
    CustomerIdentifierModel,
    CustomerLinkModel,
    CustomerProfileModel,
    MarketplaceInstallModel,
    PortalCustomerModel,
    ScheduledReleaseModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.engage import backfill, entitlement, identity
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

ADMIN, MANAGER, REP = ["admin"], ["manager"], ["case_worker"]


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    """Tenant `default`, entitled, with history and NO profiles:

    * Jane has a verified portal account (jane@x.test, phone) and two cases;
    * a case was submitted by sam@x.test, and a chat on it came from +44…222;
    * a chat from +44…999 is on no case — a number with nothing behind it;
    * tenant `other` has a case from sam@x.test and a portal account for
      jane@x.test, which must never feed tenant `default`.
    """
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default",
                        settings={"portal": {"enabled": True}})
        o = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([t, o])
        db.add(ScheduledReleaseModel(id=uuid.uuid4(), feature_key=entitlement.FEATURE_KEY,
                                     title="CE", status="released", enabled="true"))
        db.add(MarketplaceInstallModel(id=uuid.uuid4(), tenant_id="default",
                                       package_id=entitlement.PACKAGE_ID, package_version="1",
                                       package_type="module", approved_by="x"))
        for u, ten in (("ann", "default"), ("mo", "default"), ("rep", "default"),
                       ("mallory", "other")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=u, tenant_id=ten))
        ct = CaseTypeModel(id=uuid.uuid4(), name="Claims", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={"stages": []})
        db.add(ct)
        await db.flush()

        def case(tenant, n, email, name):
            return CaseInstanceModel(id=uuid.uuid4(), case_number=n, case_type_id=ct.id,
                                     case_type_version="1.0.0", status="open", priority="medium",
                                     tenant_id=tenant.id, created_by="portal", data={},
                                     portal_submitter_email=email, portal_submitter_name=name)
        j1, j2 = case(t, "C-1", "Jane@X.test", "Jane Smith"), case(t, "C-2", "jane@x.test", "Jane")
        s1 = case(t, "C-3", "sam@x.test", "Sam Lee")
        foreign = case(o, "O-1", "sam@x.test", "OTHER TENANT'S SAM")
        db.add_all([j1, j2, s1, foreign])
        db.add(PortalCustomerModel(id=uuid.uuid4(), tenant_id=t.id, primary_email="jane@x.test",
                                   display_name="Jane Smith", phone="+44 7700 900111",
                                   verified=True))
        db.add(PortalCustomerModel(id=uuid.uuid4(), tenant_id=o.id, primary_email="jane@x.test",
                                   display_name="OTHER TENANT'S JANE", verified=True))
        ep = ChannelEndpointModel(id=uuid.uuid4(), tenant_id="default", channel="sms",
                                  provider="twilio", address="+15550000000")
        db.add(ep)
        await db.flush()
        db.add(ChannelConversationModel(id=uuid.uuid4(), tenant_id="default", endpoint_id=ep.id,
                                        peer_address="+447700900222", case_id=s1.id, status="open"))
        db.add(ChannelConversationModel(id=uuid.uuid4(), tenant_id="default", endpoint_id=ep.id,
                                        peer_address="+447700900999", status="open"))
        await db.commit()
        return {"t": t, "o": o, "jane_cases": {str(j1.id), str(j2.id)}, "sam_case": str(s1.id),
                "foreign": str(foreign.id)}


async def _scan(api, roles=ADMIN, who="ann"):
    return await api.post("/api/v1/engage/backfill/runs", headers=_headers(who, roles))


async def _proposals(api, status="proposed", roles=MANAGER):
    r = await api.get("/api/v1/engage/backfill/proposals", params={"status": status},
                      headers=_headers("mo", roles))
    assert r.status_code == 200, r.text
    return r.json()["proposals"]


async def _profiles():
    async with TestSessionFactory() as db:
        return (await db.execute(select(func.count()).select_from(CustomerProfileModel))).scalar()


# ── it proposes; it applies nothing ──────────────────────────────────────────


async def test_a_scan_proposes_and_creates_nothing(api, world):
    r = await _scan(api)
    assert r.status_code == 201, r.text
    counts = r.json()["counts"]
    assert await _profiles() == 0                         # NOTHING applied
    props = {p["payload"]["primary_email"]: p for p in await _proposals(api)}
    assert set(props) == {"jane@x.test", "sam@x.test"}
    jane = props["jane@x.test"]
    assert jane["kind"] == "create_profile" and jane["payload"]["display_name"] == "Jane Smith"
    assert set(jane["payload"]["case_ids"]) == world["jane_cases"]
    kinds = {(i["kind"], i["value"]): i for i in jane["payload"]["identifiers"]}
    assert kinds[("email", "jane@x.test")]["verified"] is True     # portal OTP
    assert ("phone", "+447700900111") in kinds
    # Sam's number came from a chat ON his case — evidence about a known person.
    sam = props["sam@x.test"]
    assert ("phone", "+447700900222") in {(i["kind"], i["value"]) for i in sam["payload"]["identifiers"]}
    assert counts["numbers_skipped_no_other_evidence"] == 1


async def test_a_bare_inbound_number_never_becomes_a_person(api, world):
    await _scan(api)
    body = str(await _proposals(api))
    assert "900999" not in body


async def test_another_tenants_history_never_feeds_this_one(api, world):
    await _scan(api)
    body = str(await _proposals(api))
    assert "OTHER TENANT" not in body and world["foreign"] not in body


# ── a person accepts ─────────────────────────────────────────────────────────


async def test_accepting_creates_the_profile_with_its_evidence(api, world):
    await _scan(api)
    jane = next(p for p in await _proposals(api) if p["payload"]["primary_email"] == "jane@x.test")
    r = await api.post(f"/api/v1/engage/backfill/proposals/{jane['id']}/accept",
                       headers=_headers("mo", MANAGER), json={})
    assert r.status_code == 200, r.text
    cid = uuid.UUID(r.json()["result"]["customer_id"])
    async with TestSessionFactory() as db:
        idents = {(i.kind, i.value_normalised): i for i in (await db.execute(
            select(CustomerIdentifierModel).where(CustomerIdentifierModel.customer_id == cid))).scalars()}
        cases = {str(x) for x in (await db.execute(select(CustomerLinkModel.object_id).where(
            CustomerLinkModel.customer_id == cid, CustomerLinkModel.object_kind == "case"))).scalars()}
        profile = await db.get(CustomerProfileModel, cid)
    assert profile.source == "backfill"
    assert idents[("email", "jane@x.test")].verified and idents[("email", "jane@x.test")].is_primary
    assert not idents[("phone", "+447700900111")].verified      # never promoted
    assert cases == world["jane_cases"]


async def test_a_rerun_after_accepting_proposes_only_what_is_new(api, world):
    await _scan(api)
    for p in await _proposals(api):
        await api.post(f"/api/v1/engage/backfill/proposals/{p['id']}/accept",
                       headers=_headers("mo", MANAGER), json={})
    r = await _scan(api)
    assert r.json()["counts"]["proposed_new"] == 0, r.json()
    assert await _proposals(api) == []
    # New history arrives: another case from Jane → an ENRICH, not a new person.
    async with TestSessionFactory() as db:
        ct = (await db.execute(select(CaseTypeModel))).scalars().first()
        db.add(CaseInstanceModel(id=uuid.uuid4(), case_number="C-9", case_type_id=ct.id,
                                 case_type_version="1.0.0", status="open", priority="medium",
                                 tenant_id=world["t"].id, created_by="portal", data={},
                                 portal_submitter_email="jane@x.test"))
        await db.commit()
    await _scan(api)
    props = await _proposals(api)
    assert [p["kind"] for p in props] == ["enrich_profile"], props
    assert len(props[0]["payload"]["case_ids"]) == 1 and props[0]["payload"]["identifiers"] == []


async def test_a_rejection_stands_across_reruns(api, world):
    await _scan(api)
    sam = next(p for p in await _proposals(api) if p["payload"]["primary_email"] == "sam@x.test")
    r = await api.post(f"/api/v1/engage/backfill/proposals/{sam['id']}/reject",
                       headers=_headers("mo", MANAGER), json={"reason": "test account"})
    assert r.status_code == 200, r.text
    r = await _scan(api)
    assert r.json()["counts"]["rejected_not_reproposed"] == 1
    assert all(p["payload"]["primary_email"] != "sam@x.test" for p in await _proposals(api))


async def test_a_rejection_needs_a_reason(api, world):
    await _scan(api)
    p = (await _proposals(api))[0]
    r = await api.post(f"/api/v1/engage/backfill/proposals/{p['id']}/reject",
                       headers=_headers("mo", MANAGER), json={})
    assert r.status_code == 422, r.text


async def test_duplicates_become_a_merge_proposal_never_a_merge(api, world):
    """Two profiles already hold halves of Jane. The scan proposes a merge; it
    does not perform one, and accepting needs the merge privilege and a reason."""
    async with TestSessionFactory() as db:
        a = await identity.create_profile(db, tenant_uuid=world["t"].id, display_name="J Smith")
        await identity.add_identifier(db, tenant_uuid=world["t"].id, customer_id=a.id,
                                      kind="email", value="jane@x.test")
        b = await identity.create_profile(db, tenant_uuid=world["t"].id, display_name="Jane S")
        await identity.add_identifier(db, tenant_uuid=world["t"].id, customer_id=b.id,
                                      kind="phone", value="+447700900111")
        await db.commit()
    await _scan(api)
    merge = next(p for p in await _proposals(api) if p["kind"] == "merge_profiles")
    assert {merge["payload"]["winner_id"], *merge["payload"]["loser_ids"]} == {str(a.id), str(b.id)}
    async with TestSessionFactory() as db:
        assert (await db.get(CustomerProfileModel, b.id)).status == "active"   # untouched
    url = f"/api/v1/engage/backfill/proposals/{merge['id']}/accept"
    assert (await api.post(url, headers=_headers("mo", MANAGER), json={})).status_code == 422
    r = await api.post(url, headers=_headers("mo", MANAGER), json={"reason": "same person"})
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        assert (await db.get(CustomerProfileModel, b.id)).status == "merged"


async def test_accept_rechecks_against_the_data_as_it_is_now(api, world):
    """Between the scan and the accept, a rep gave Jane's phone to someone else.
    The accept refuses rather than moving it, and the proposal is superseded."""
    await _scan(api)
    jane = next(p for p in await _proposals(api) if p["payload"]["primary_email"] == "jane@x.test")
    async with TestSessionFactory() as db:
        x = await identity.create_profile(db, tenant_uuid=world["t"].id, display_name="Someone")
        await identity.add_identifier(db, tenant_uuid=world["t"].id, customer_id=x.id,
                                      kind="phone", value="+447700900111")
        await db.commit()
    r = await api.post(f"/api/v1/engage/backfill/proposals/{jane['id']}/accept",
                       headers=_headers("mo", MANAGER), json={})
    assert r.status_code == 409 and "Re-run" in r.text, r.text
    assert await _profiles() == 1                           # nothing half-applied
    assert any(p["id"] == jane["id"] for p in await _proposals(api, status="superseded"))


# ── who may ──────────────────────────────────────────────────────────────────


async def test_who_may_run_and_decide(api, world):
    assert (await _scan(api, roles=MANAGER, who="mo")).status_code == 403      # admin only
    assert (await _scan(api, roles=REP, who="rep")).status_code == 403
    await _scan(api)
    p = (await _proposals(api))[0]
    r = await api.post(f"/api/v1/engage/backfill/proposals/{p['id']}/accept",
                       headers=_headers("rep", REP), json={})
    assert r.status_code == 403, r.text
    r = await api.get("/api/v1/engage/backfill/proposals", headers=_headers("rep", REP))
    assert r.status_code == 403, r.text


async def test_another_tenant_cannot_see_or_decide_a_proposal(api, world):
    await _scan(api)
    p = (await _proposals(api))[0]
    r = await api.post(f"/api/v1/engage/backfill/proposals/{p['id']}/accept",
                       headers=_headers("mallory", ADMIN), json={})
    assert r.status_code == 404, r.text


# ── the ladder the backfill reads from ───────────────────────────────────────


async def test_the_ladder_never_adopts_another_tenants_customer(world):
    """Before the fix, a screen-pop in `default` for sam@x.test could adopt a
    profile named after `other`'s submitter; for a number seen only in `other`'s
    chats, likewise."""
    async with TestSessionFactory() as db:
        out = await identity.resolve(db, tenant_uuid=world["o"].id, tenant_token="other",
                                     kind="email", value="jane@x.test", adopt=False)
        # `other` has its OWN portal Jane — found as other's, never default's.
        assert out["candidate"]["display_name"] == "OTHER TENANT'S JANE"
        out = await identity.resolve(db, tenant_uuid=world["o"].id, tenant_token="other",
                                     kind="phone", value="+447700900222", adopt=False)
        assert out["rung"] == 5, out                 # default's chat is not other's


async def test_rung_two_matches_by_phone_and_case_insensitively(world):
    async with TestSessionFactory() as db:
        out = await identity.resolve(db, tenant_uuid=world["t"].id, tenant_token="default",
                                     kind="phone", value="07700 900111".replace("0", "+44", 1),
                                     adopt=False)
        assert out["rung"] == 2 and out["candidate"]["display_name"] == "Jane Smith", out
        out = await identity.resolve(db, tenant_uuid=world["t"].id, tenant_token="default",
                                     kind="email", value="JANE@X.TEST", adopt=False)
        assert out["rung"] == 2, out


# ── erasure reaches the rep desk ─────────────────────────────────────────────


async def test_portal_erasure_forgets_the_profile_and_the_proposals(api, world, monkeypatch):
    """Found building this item: erasing a portal account left the rep desk's
    profile — name, email, phone, call numbers — and the backfill's proposals
    quoting them. Now the same erasure reaches all of it."""
    await _scan(api)
    jane = next(p for p in await _proposals(api) if p["payload"]["primary_email"] == "jane@x.test")
    r = await api.post(f"/api/v1/engage/backfill/proposals/{jane['id']}/accept",
                       headers=_headers("mo", MANAGER), json={})
    cid = uuid.UUID(r.json()["result"]["customer_id"])
    await _scan(api)       # leaves a pending proposal or two that may quote her
    # The portal reads the shared flag cache (R5e step 8 — it leaves case-service).
    from velaris_shared.core import feature_flags
    monkeypatch.setattr(feature_flags, "is_feature_enabled", lambda key: True)
    async with TestSessionFactory() as db:
        pc = (await db.execute(select(PortalCustomerModel).where(
            PortalCustomerModel.tenant_id == world["t"].id))).scalars().one()
    r = await api.delete(f"/api/v1/portal/default/customers/{pc.id}",
                         headers=_headers("ann", ADMIN))
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        profile = await db.get(CustomerProfileModel, cid)
        idents = (await db.execute(select(CustomerIdentifierModel).where(
            CustomerIdentifierModel.customer_id == cid))).scalars().all()
        proposals = (await db.execute(select(CustomerBackfillProposalModel))).scalars().all()
    assert profile.status == "anonymized" and profile.display_name == "Erased customer"
    assert idents == []
    blob = str([(p.payload, p.evidence, p.result) for p in proposals]).lower()
    assert "jane" not in blob and "900111" not in blob, blob


async def test_a_profile_can_be_erased_from_the_rep_desk(api, world):
    async with TestSessionFactory() as db:
        p = await identity.create_profile(db, tenant_uuid=world["t"].id, display_name="Walk In")
        await identity.add_identifier(db, tenant_uuid=world["t"].id, customer_id=p.id,
                                      kind="phone", value="+447700900333")
        await db.commit()
    url = f"/api/v1/engage/profiles/{p.id}/erase"
    assert (await api.post(url, headers=_headers("mo", MANAGER), json={"reason": "asked"})
            ).status_code == 403                                   # admin only
    r = await api.post(url, headers=_headers("ann", ADMIN), json={"reason": "customer asked"})
    assert r.status_code == 200 and r.json()["profiles_anonymized"] == 1, r.text
    async with TestSessionFactory() as db:
        assert (await db.execute(select(CustomerIdentifierModel).where(
            CustomerIdentifierModel.customer_id == p.id))).scalars().all() == []


async def test_an_erased_portal_account_proposes_nobody(api, world):
    async with TestSessionFactory() as db:
        db.add(PortalCustomerModel(id=uuid.uuid4(), tenant_id=world["t"].id,
                                   primary_email="deleted_123@deleted", display_name="Deleted User"))
        await db.commit()
    r = await _scan(api)
    assert r.json()["counts"]["skipped_erased"] == 1
    assert "Deleted User" not in str(await _proposals(api))


async def test_a_superseded_proposal_keeps_no_personal_data(api, world):
    await _scan(api)
    async with TestSessionFactory() as db:          # Sam's case goes away
        for c in (await db.execute(select(CaseInstanceModel).where(
                CaseInstanceModel.portal_submitter_email == "sam@x.test"))).scalars().all():
            c.portal_submitter_email = None
        await db.commit()
    await _scan(api)
    gone = await _proposals(api, status="superseded")
    assert gone and all(p["payload"] == {} and p["evidence"] == {} for p in gone)
