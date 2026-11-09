"""Oct item 4 — relationships from an HxGraph traversal.

Pinned: each kind of evidence joins the right people and says why; the walk
stops at two hops and never walks through the weak evidence; a switchboard or a
webmail domain relates nobody; another tenant's, a merged-away or an erased
person is never suggested; the suggestion is labelled so and never
`may_act_for`; it appears only at Full and under the relations panel's grant;
and no raw number or full name of the other person reaches the rep.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    CustomerInteractionModel,
    CustomerProfileModel,
    MarketplaceInstallModel,
    ScheduledReleaseModel,
    TenantModel,
    UserDirectoryModel,
)
from velaris_shared.hxgraph import customer_graph

from case_service.engage import entitlement, identity, interactions
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

LINE = "+447700900500"
SWITCHBOARD = "+442071234567"


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _person(db, t, name, email=None):
    p = await identity.create_profile(db, tenant_uuid=t.id, display_name=name)
    if email:
        await identity.add_identifier(db, tenant_uuid=t.id, customer_id=p.id, kind="email", value=email)
    return p


async def _call(db, t, p, ani):
    db.add(CustomerInteractionModel(id=uuid.uuid4(), tenant_id=t.id, customer_id=p.id,
                                    channel="voice", ani=ani, status="wrapped"))


@pytest_asyncio.fixture
async def world():
    """Jane (the caller). Bob calls from Jane's line. Carol is on Jane's claim.
    Dan works at Jane's small firm. Fay is on Bob's claim (two hops from Jane).
    Gus shares only webmail. Ten people call from one switchboard with Jane.
    Mo, in another tenant, calls from Jane's line too."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        o = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([t, o])
        db.add(ScheduledReleaseModel(id=uuid.uuid4(), feature_key=entitlement.FEATURE_KEY,
                                     title="CE", status="released", enabled="true"))
        db.add(MarketplaceInstallModel(id=uuid.uuid4(), tenant_id="default",
                                       package_id=entitlement.PACKAGE_ID, package_version="1",
                                       package_type="module", approved_by="x"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="rep", tenant_id="default"))
        ct = CaseTypeModel(id=uuid.uuid4(), name="Claims", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={"stages": []})
        db.add(ct)
        await db.flush()
        jane = await _person(db, t, "Jane Smith", "jane@smallfirm.test")
        bob = await _person(db, t, "Bob Smith")
        carol = await _person(db, t, "Carol Jones")
        dan = await _person(db, t, "Dan Brown", "dan@smallfirm.test")
        fay = await _person(db, t, "Fay Green")
        gus = await _person(db, t, "Gus Grey", "gus@gmail.com")
        await identity.add_identifier(db, tenant_uuid=t.id, customer_id=jane.id,
                                      kind="email", value="jane.personal@gmail.com")
        mo = await identity.create_profile(db, tenant_uuid=o.id, display_name="Mo Other")

        def case(n):
            return CaseInstanceModel(id=uuid.uuid4(), case_number=n, case_type_id=ct.id,
                                     case_type_version="1.0.0", status="open", priority="medium",
                                     tenant_id=t.id, created_by="x", data={})
        claim, bobs = case("CLM-1"), case("CLM-2")
        db.add_all([claim, bobs])
        await db.flush()
        for p, c in ((jane, claim), (carol, claim), (bob, bobs), (fay, bobs)):
            await identity.link(db, tenant_uuid=t.id, customer_id=p.id,
                                object_kind="case", object_id=c.id)
        await _call(db, t, jane, LINE)
        await _call(db, t, bob, LINE)
        db.add(CustomerInteractionModel(id=uuid.uuid4(), tenant_id=o.id, customer_id=mo.id,
                                        channel="voice", ani=LINE, status="wrapped"))
        await _call(db, t, jane, SWITCHBOARD)
        crowd = []
        for i in range(10):
            p = await _person(db, t, f"Crowd {i}")
            await _call(db, t, p, SWITCHBOARD)
            crowd.append(p)
        await db.commit()
        return {"t": t, "jane": jane, "bob": bob, "carol": carol, "dan": dan, "fay": fay,
                "gus": gus, "mo": mo, "crowd": crowd}


async def _related(world):
    async with TestSessionFactory() as db:
        return await customer_graph.related(db, tenant_uuid=world["t"].id,
                                            customer_id=world["jane"].id)


def _by_id(out):
    return {s["customer_id"]: s for s in out["suggestions"]}


async def test_each_kind_of_evidence_joins_the_right_people(world):
    out = _by_id(await _related(world))
    assert out[str(world["bob"].id)]["suggested"] == "shares a line"
    assert out[str(world["carol"].id)]["suggested"] == "co-party"
    assert out[str(world["dan"].id)]["suggested"] == "same organisation"
    assert out[str(world["carol"].id)]["because"] == ["linked to the same case"]


async def test_two_hops_through_strong_evidence_only(world):
    out = _by_id(await _related(world))
    fay = out[str(world["fay"].id)]                    # Jane -line- Bob -claim- Fay
    assert fay["hops"] == 2 and fay["score"] < out[str(world["bob"].id)]["score"]
    assert "through" in fay["because"][0]


async def test_a_switchboard_and_webmail_relate_nobody(world):
    out = await _related(world)
    ids = set(_by_id(out))
    assert not ({str(p.id) for p in world["crowd"]} & ids)
    assert str(world["gus"].id) not in ids              # gmail.com says nothing
    assert out["skipped_as_too_common"]["number"] == 1


async def test_another_tenant_is_never_suggested(world):
    assert str(world["mo"].id) not in _by_id(await _related(world))


async def test_merged_or_erased_people_are_not_suggested(world):
    from case_service.engage import erasure

    async with TestSessionFactory() as db:
        await erasure.forget(db, tenant_uuid=world["t"].id, profile_ids=[world["carol"].id],
                             reason="asked")
        await identity.merge(db, tenant_uuid=world["t"].id, winner_id=world["jane"].id,
                             loser_id=world["dan"].id, reason="same person", merged_by="x")
        await db.commit()
    ids = set(_by_id(await _related(world)))
    assert str(world["carol"].id) not in ids and str(world["dan"].id) not in ids


async def test_a_suggestion_is_labelled_masked_and_never_may_act_for(world):
    out = await _related(world)
    for s in out["suggestions"]:
        assert s["suggestion"] is True and s["may_act_for"] is False
        assert "Smith" not in s["display_name_masked"]
    assert LINE not in str(out) and LINE[-6:] not in str(out)


async def test_it_appears_only_at_full_and_a_confirmed_one_is_marked(api, world):
    async with TestSessionFactory() as db:
        pop = await interactions.screen_pop(db, tenant_uuid=world["t"].id, tenant_token="default",
                                            channel="voice", ani=None)
        call = await db.get(CustomerInteractionModel, uuid.UUID(pop["interaction_id"]))
        call.customer_id = world["jane"].id
        await db.commit()
    h = _headers("rep", ["case_worker"])
    url = f"/api/v1/engage/profiles/{world['jane'].id}/360"
    r = await api.get(url, params={"interaction_id": str(call.id)}, headers=h)
    body = r.json()
    assert "suggested_relations" not in body["panels"]
    assert "suggested_relations" in [w["panel"] for w in body["withheld"]]
    async with TestSessionFactory() as db:
        c = await db.get(CustomerInteractionModel, call.id)
        await interactions.verify(db, interaction=c, factor="otp", ok=True, to_tier="full")
        await db.commit()
    body = (await api.get(url, params={"interaction_id": str(call.id)}, headers=h)).json()
    assert str(world["bob"].id) in _by_id(body["panels"]["suggested_relations"])
    # A rep who agrees records it through the ordinary route — never may_act_for.
    r = await api.post(f"/api/v1/engage/profiles/{world['jane'].id}/relations", headers=h,
                       json={"to_customer_id": str(world["bob"].id), "relation": "related_to",
                             "note": "confirmed from suggestion"})
    assert r.status_code == 201, r.text
    body = (await api.get(url, params={"interaction_id": str(call.id)}, headers=h)).json()
    assert _by_id(body["panels"]["suggested_relations"])[str(world["bob"].id)]["already_related"]


async def test_a_reader_without_the_grant_gets_nothing(api, world):
    r = await api.get(f"/api/v1/engage/profiles/{world['jane'].id}/360",
                      headers=_headers("rep", ["viewer"]))
    assert r.status_code == 403, r.text


async def test_the_walk_is_bounded_in_cost(world, monkeypatch):
    """It runs on every Full-tier 360: a well-connected customer must not turn a
    page view into thousands of queries."""
    monkeypatch.setattr(customer_graph, "MAX_EVIDENCE", 2)
    out = await _related(world)
    assert out["truncated"] is True


async def test_confirming_twice_records_once(api, world):
    h = _headers("rep", ["case_worker"])
    body = {"to_customer_id": str(world["bob"].id), "relation": "related_to"}
    a = await api.post(f"/api/v1/engage/profiles/{world['jane'].id}/relations", headers=h, json=body)
    b = await api.post(f"/api/v1/engage/profiles/{world['jane'].id}/relations", headers=h, json=body)
    assert a.json()["relation"]["id"] == b.json()["relation"]["id"]


async def test_another_tenant_cannot_name_a_person_here(api, world):
    async with TestSessionFactory() as db:
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="mal", tenant_id="other"))
        await db.commit()
    r = await api.post(f"/api/v1/engage/profiles/{world['mo'].id}/relations",
                       headers=_headers("mal", ["admin"]),
                       json={"to_customer_id": str(world["jane"].id), "relation": "related_to"})
    assert r.status_code == 404, r.text



async def test_no_reason_quotes_a_case_number_or_a_third_partys_line(world):
    """Found in the audit: reasons said "both on case CLM-2" — for a second hop,
    that is a case of two OTHER people, which the rep may not be allowed to open
    — and quoted the digits of lines that are not the caller's."""
    async with TestSessionFactory() as db:
        other_line = "+447700900777"
        await _call(db, world["t"], world["bob"], other_line)
        eve = await _person(db, world["t"], "Eve Stone")
        await _call(db, world["t"], eve, other_line)      # Eve shares BOB's line
        await db.commit()
    out = await _related(world)
    blob = str(out)
    assert "CLM-" not in blob
    assert "777" not in blob
    eve_s = _by_id(out)[str(eve.id)]
    assert eve_s["hops"] == 2 and eve_s["because"] == [
        "connected through someone who shares their line"]


async def test_a_domain_is_matched_literally_not_as_a_pattern(world):
    async with TestSessionFactory() as db:
        a = await _person(db, world["t"], "Ann Under", "ann@my_firm.test")
        b = await _person(db, world["t"], "Bea Other", "bea@myxfirm.test")
        await db.commit()
        out = await customer_graph.related(db, tenant_uuid=world["t"].id, customer_id=a.id)
    assert str(b.id) not in _by_id(out)
