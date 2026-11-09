"""Oct item 1 — a join link for the next rep on a live contact.

The item's rules, each pinned:

* single-use, short-lived, bound to a staff identity — never a guest, never
  across tenants, and every way a link is wrong gives the same answer;
* joining carries the verification tier, as transfer does, and needs
  `engage.interactions.write`;
* warm (both reps on the contact) vs cold (the first leaves when the second
  joins) — and the record and the analytics say two reps handled it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    CustomerInteractionJoinModel,
    CustomerInteractionModel,
    MarketplaceInstallModel,
    ScheduledReleaseModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.engage import entitlement, identity, insights, interactions, joins
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

REP = ["case_worker"]


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    """Tenant `default`, entitled. alice has Jane on the phone; bob and carol are
    colleagues; mallory is in another tenant."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        other = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([t, other])
        db.add(ScheduledReleaseModel(id=uuid.uuid4(), feature_key=entitlement.FEATURE_KEY,
                                     title="CE", status="released", enabled="true"))
        db.add(MarketplaceInstallModel(id=uuid.uuid4(), tenant_id="default",
                                       package_id=entitlement.PACKAGE_ID, package_version="1",
                                       package_type="module", approved_by="x"))
        for u, ten in (("alice", "default"), ("bob", "default"), ("carol", "default"),
                       ("mallory", "other")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=u, tenant_id=ten))
        await db.commit()
        jane = await identity.create_profile(db, tenant_uuid=t.id, display_name="Jane")
        await identity.add_identifier(db, tenant_uuid=t.id, customer_id=jane.id,
                                      kind="phone", value="+447700900555")
        await db.commit()
        pop = await interactions.screen_pop(db, tenant_uuid=t.id, tenant_token="default",
                                            channel="voice", ani="+447700900555",
                                            agent_user_id="alice")
        row = await db.get(CustomerInteractionModel, uuid.UUID(pop["interaction_id"]))
        await interactions.verify(db, interaction=row, factor="otp", ok=True, to_tier="full")
        await db.commit()
        return {"tenant": t, "jane": jane, "call": uuid.UUID(pop["interaction_id"])}


async def _link(api, world, *, mode="warm", **target):
    body = {"mode": mode, **(target or {"for_user_id": "bob"})}
    r = await api.post(f"/api/v1/engage/interactions/{world['call']}/join-links",
                       headers=_headers("alice", REP), json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _redeem(api, who, token, roles=REP):
    return await api.post("/api/v1/engage/join-links/redeem",
                          headers=_headers(who, roles), json={"token": token})


# ── the happy path ───────────────────────────────────────────────────────────


async def test_bob_joins_warm_with_the_tier_and_alice_stays(api, world):
    link = await _link(api, world)
    assert link["path"].startswith("/rep-desk#join=")   # never in a server log
    r = await _redeem(api, "bob", link["token"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["customer_id"] == str(world["jane"].id)
    assert out["verification_level"] == "full"          # not re-verified
    assert out["mode"] == "warm" and out["joined_from"] == str(world["call"])
    async with TestSessionFactory() as db:
        joined = await db.get(CustomerInteractionModel, uuid.UUID(out["interaction_id"]))
        origin = await db.get(CustomerInteractionModel, world["call"])
    assert joined.agent_user_id == "bob" and joined.handoff_kind == "warm"
    assert joined.transferred_from == origin.id
    assert origin.status == "open"                      # both reps on the contact
    # And bob's 360 opens on that interaction, at the inherited tier.
    r = await api.get(f"/api/v1/engage/profiles/{world['jane'].id}/360",
                      params={"interaction_id": out["interaction_id"]},
                      headers=_headers("bob", REP))
    assert r.status_code == 200 and r.json()["verification"] == "full", r.text


async def test_cold_hands_over_only_when_bob_joins(api, world):
    link = await _link(api, world, mode="cold")
    async with TestSessionFactory() as db:
        assert (await db.get(CustomerInteractionModel, world["call"])).status == "open"
    r = await _redeem(api, "bob", link["token"])
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        origin = await db.get(CustomerInteractionModel, world["call"])
    assert origin.status == "wrapped" and origin.outcome == "handed_over"
    # A contact that has ended cannot be joined again.
    r = await api.post(f"/api/v1/engage/interactions/{world['call']}/join-links",
                       headers=_headers("alice", REP), json={"for_user_id": "carol"})
    assert r.status_code == 409, r.text


async def test_a_queue_link_admits_a_rep_the_queue_admits(api, world):
    link = await _link(api, world, for_queue="billing")
    assert (await _redeem(api, "carol", link["token"])).status_code == 200


async def test_a_queue_restricted_group_is_refused():
    class G:
        allowed_queue_ids = ["some-other-queue"]

    class U:
        active_access_group = G()

    async with TestSessionFactory() as db:
        assert await joins._queue_admits(db, U(), "billing", uuid.uuid4()) is False
        G.allowed_queue_ids = ["billing"]
        assert await joins._queue_admits(db, U(), "billing", uuid.uuid4()) is True


# ── the credential ───────────────────────────────────────────────────────────


async def test_the_token_is_stored_only_as_a_hash_and_listed_without_it(api, world):
    link = await _link(api, world)
    async with TestSessionFactory() as db:
        row = (await db.execute(select(CustomerInteractionJoinModel))).scalars().one()
    assert row.token_hash == joins.hash_token(link["token"])
    assert link["token"] not in str(row.__dict__)
    r = await api.get(f"/api/v1/engage/interactions/{world['call']}/join-links",
                      headers=_headers("alice", REP))
    assert r.status_code == 200 and "token" not in r.json()["links"][0], r.text


async def test_it_works_once(api, world):
    link = await _link(api, world)
    assert (await _redeem(api, "bob", link["token"])).status_code == 200
    r = await _redeem(api, "bob", link["token"])
    assert r.status_code == 404 and joins.NOT_VALID in r.text


async def test_every_wrong_link_gives_the_same_answer(api, world):
    """Unknown, somebody else's, the creator's own, expired, revoked, another
    tenant's — one sentence, so a guesser learns nothing."""
    link = await _link(api, world)
    answers = {
        "unknown": await _redeem(api, "bob", "x" * 43),
        "not for carol": await _redeem(api, "carol", link["token"]),
        "the creator": await _redeem(api, "alice", link["token"]),
        "another tenant": await _redeem(api, "mallory", link["token"]),
    }
    expired = await _link(api, world)
    async with TestSessionFactory() as db:
        row = (await db.execute(select(CustomerInteractionJoinModel).where(
            CustomerInteractionJoinModel.token_hash == joins.hash_token(expired["token"])
        ))).scalars().one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    answers["expired"] = await _redeem(api, "bob", expired["token"])
    revoked = await _link(api, world)
    r = await api.delete(f"/api/v1/engage/join-links/{revoked['id']}", headers=_headers("alice", REP))
    assert r.status_code == 200 and r.json()["state"] == "revoked", r.text
    answers["revoked"] = await _redeem(api, "bob", revoked["token"])
    for why, resp in answers.items():
        # Mallory's tenant is not entitled, so she is refused one gate earlier —
        # still a 404, still nothing about the link.
        assert resp.status_code == 404, (why, resp.text)
        if why != "another tenant":
            assert joins.NOT_VALID in resp.text, (why, resp.text)
    # And the link bob WAS given still works after all that.
    assert (await _redeem(api, "bob", link["token"])).status_code == 200


async def test_joining_needs_the_interactions_write_privilege(api, world):
    link = await _link(api, world)
    r = await _redeem(api, "bob", link["token"], roles=["viewer"])
    assert r.status_code == 403, r.text


async def test_a_link_names_one_colleague_in_this_tenant(api, world):
    url = f"/api/v1/engage/interactions/{world['call']}/join-links"
    h = _headers("alice", REP)
    for body in ({"for_user_id": "mallory"},            # another tenant's rep
                 {"for_user_id": "nobody"},
                 {"for_user_id": "alice"},              # yourself
                 {"for_user_id": "bob", "for_queue": "billing"},
                 {},
                 {"for_user_id": "bob", "mode": "sideways"}):
        r = await api.post(url, headers=h, json=body)
        assert r.status_code == 422, (body, r.text)
    # The same answer for "nobody" and "another tenant's rep".
    a = await api.post(url, headers=h, json={"for_user_id": "mallory"})
    b = await api.post(url, headers=h, json={"for_user_id": "nobody"})
    assert a.json()["detail"] == b.json()["detail"]


async def test_another_tenant_cannot_mint_a_link_on_this_contact(api, world):
    r = await api.post(f"/api/v1/engage/interactions/{world['call']}/join-links",
                       headers=_headers("mallory", ["admin"]), json={"for_user_id": "mallory"})
    assert r.status_code == 404, r.text


async def test_a_used_link_cannot_be_revoked(api, world):
    link = await _link(api, world)
    await _redeem(api, "bob", link["token"])
    r = await api.delete(f"/api/v1/engage/join-links/{link['id']}", headers=_headers("alice", REP))
    assert r.status_code == 409, r.text


# ── the record and the numbers ───────────────────────────────────────────────


async def test_analytics_say_two_reps_handled_the_contact(api, world):
    await _redeem(api, "bob", (await _link(api, world))["token"])
    async with TestSessionFactory() as db:
        out = await insights.analytics(db, tenant_uuid=world["tenant"].id)
    assert out["handoffs"] == {"transfer": 0, "warm": 1, "cold": 0}
    assert out["multi_rep_contact_rate"] == 1.0
    assert "multi_rep_contact_rate" in out["definitions"]


async def test_a_plain_transfer_is_now_labelled_too(world):
    async with TestSessionFactory() as db:
        row = await db.get(CustomerInteractionModel, world["call"])
        new = await interactions.transfer(db, tenant_uuid=world["tenant"].id,
                                          interaction=row, to_agent="bob")
        await db.commit()
        assert new.handoff_kind == "transfer"


async def test_only_the_rep_on_the_contact_or_a_supervisor_brings_someone_in(api, world):
    """Found in the audit: any rep in the tenant could pull a colleague's caller
    onto a third desk, or cancel another rep's handover."""
    url = f"/api/v1/engage/interactions/{world['call']}/join-links"
    r = await api.post(url, headers=_headers("carol", REP), json={"for_user_id": "bob"})
    assert r.status_code == 403, r.text
    link = await _link(api, world)
    r = await api.delete(f"/api/v1/engage/join-links/{link['id']}", headers=_headers("carol", REP))
    assert r.status_code == 403, r.text
    # A supervisor may do both.
    r = await api.post(url, headers=_headers("carol", ["manager"]), json={"for_user_id": "bob"})
    assert r.status_code == 201, r.text
    r = await api.delete(f"/api/v1/engage/join-links/{link['id']}", headers=_headers("carol", ["manager"]))
    assert r.status_code == 200, r.text


import os  # noqa: E402

_PG = pytest.mark.skipif(
    not os.environ.get("VELARIS_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="row locks need a real PostgreSQL; SQLite ignores FOR UPDATE")


@_PG
async def test_two_tabs_racing_one_link_cannot_both_join(api, world):
    """The redeem locks the link row first, so of two concurrent opens exactly
    one wins and the other gets the one answer every used link gets."""
    import asyncio

    from velaris_shared.auth.models import AuthenticatedUser

    link = await _link(api, world)
    bob = AuthenticatedUser(user_id="bob", roles=REP, tenant_id="default")

    async def attempt():
        async with TestSessionFactory() as db:
            try:
                await joins.redeem(db, tenant_uuid=world["tenant"].id,
                                   token=link["token"], actor=bob)
                await asyncio.sleep(0.3)        # hold the lock across the race
                await db.commit()
                return "won"
            except joins.JoinError as exc:
                await db.rollback()
                return str(exc)

    results = sorted(await asyncio.gather(attempt(), attempt()))
    assert results == [joins.NOT_VALID, "won"], results
    async with TestSessionFactory() as db:
        joined = (await db.execute(select(CustomerInteractionModel).where(
            CustomerInteractionModel.transferred_from == world["call"]))).scalars().all()
    assert len(joined) == 1
