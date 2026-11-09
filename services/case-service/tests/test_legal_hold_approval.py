"""Legal hold release — the optional second person (mig 218, 2026-10-14).

Pinned here:

* **off by default** — a tenant with no approving group releases at once,
  exactly as before 218, and nothing is asked for;
* **on** — a release is ASKED for (202), the case stays held, and only an
  active member of the named group who is not the asker can approve it;
* **refusing** — a member refuses, the asker withdraws, anyone else is 403;
* **the rule cannot be switched off by the person it binds** — changing or
  clearing a named group needs membership of it, or the superadmin;
* **fail closed** — a group made inactive after it was named makes releases
  wait; it does not turn the rule off;
* **tenants** — another tenant's group cannot be named, another tenant's hold
  cannot be approved;
* **without migration 218's tables** releases behave exactly as before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    AccessGroupModel, CaseAuditLogModel, LegalHoldModel, LegalHoldReleaseRequestModel,
    OperatorAccessGroupModel, PortalModel, SecurityEventModel, UserDirectoryModel,
)

from case_service.legal_hold import approval as la
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from tests.test_legal_hold import (  # noqa: F401 — fixtures
    ALICE, BOB, HOLDS, MALLORY, REASON, SAM, SUPER, _customer_accounts_on,
    _fresh_table_probe, _hold, _headers, _release, _rows, api, world,
)

pytestmark = pytest.mark.asyncio

CARL = _headers("carl", ["admin"])     # acme admin AND a compliance member
SETTINGS = f"{HOLDS}/settings"


@pytest.fixture(autouse=True)
def _fresh_approval_probe():
    la._reset_cache_for_tests()
    yield
    la._reset_cache_for_tests()


@pytest_asyncio.fixture
async def groups(world):
    """acme: `compliance` (sam, carl) and `dormant` (inactive). evil: `theirs`."""
    async with TestSessionFactory() as db:
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="carl", tenant_id="acme"))
        portal = PortalModel(id=uuid.uuid4(), name="Staff", portal_type="staff")
        db.add(portal)
        await db.flush()
        comp = AccessGroupModel(id=uuid.uuid4(), name="Compliance", tenant_id="acme",
                                portal_id=portal.id, is_active=True)
        dormant = AccessGroupModel(id=uuid.uuid4(), name="Dormant", tenant_id="acme",
                                   portal_id=portal.id, is_active=False)
        theirs = AccessGroupModel(id=uuid.uuid4(), name="Evil team", tenant_id="evil",
                                  portal_id=portal.id, is_active=True)
        db.add_all([comp, dormant, theirs])
        await db.flush()
        for who in ("sam", "carl"):
            db.add(OperatorAccessGroupModel(id=uuid.uuid4(), operator_id=who,
                                            access_group_id=comp.id))
        await db.commit()
    return {"comp": comp, "dormant": dormant, "theirs": theirs}


async def _name_group(api, group_id, headers=ALICE):
    return await api.put(SETTINGS, headers=headers, json={
        "approver_group_id": str(group_id) if group_id else None})


async def _approve(api, hold, headers, note=None):
    return await api.post(f"{HOLDS}/{hold}/release/approve", headers=headers,
                          json={"note": note} if note else {})


async def _reject(api, hold, headers, note="Not yet — the appeal window is open."):
    return await api.post(f"{HOLDS}/{hold}/release/reject", headers=headers, json={"note": note})


async def _hold_row(hold_id):
    async with TestSessionFactory() as db:
        return await db.get(LegalHoldModel, uuid.UUID(hold_id))


# ── off by default ───────────────────────────────────────────────────────────


async def test_no_group_named_releases_at_once_as_before(api, world, groups):
    hold = await _hold(api, world["held"])
    r = await _release(api, hold)
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is False and r.json()["case_still_held"] is False
    assert await _rows(LegalHoldReleaseRequestModel) == []
    s = (await api.get(SETTINGS, headers=ALICE)).json()
    assert s["approver_group_id"] is None and s["can_configure"] and s["can_change"]


# ── on ───────────────────────────────────────────────────────────────────────


async def test_a_release_waits_for_a_second_member(api, world, groups):
    assert (await _name_group(api, groups["comp"].id)).status_code == 200
    hold = await _hold(api, world["held"])

    r = await _release(api, hold)                         # alice asks
    assert r.status_code == 202, r.text
    assert r.json()["pending"] is True and r.json()["case_still_held"] is True
    assert (await _hold_row(hold)).status == "active"

    # a second ask while one is open is refused
    assert (await _release(api, hold)).status_code == 409

    # the asker cannot approve; a non-member cannot; another tenant cannot see it
    assert (await _approve(api, hold, ALICE)).json()["error"]["code"] == la.CODE_SELF
    assert (await _approve(api, hold, BOB)).json()["error"]["code"] == la.CODE_NOT_APPROVER
    assert (await _approve(api, hold, MALLORY)).status_code == 404
    assert (await _hold_row(hold)).status == "active"

    # what each person is shown
    listed = {h["id"]: h for h in (await api.get(f"{HOLDS}/cases/{world['held'].id}",
                                                 headers=SAM)).json()["holds"]}
    assert listed[hold]["can_approve"] is True and listed[hold]["pending_release"]
    mine = (await api.get(HOLDS, headers=ALICE)).json()["holds"][0]
    assert mine["can_approve"] is False and mine["can_withdraw"] is True

    r = await _approve(api, hold, SAM, note="Checked with counsel.")
    assert r.status_code == 200, r.text
    assert r.json()["case_still_held"] is False
    row = await _hold_row(hold)
    assert row.status == "released" and row.released_by == "alice"
    req = (await _rows(LegalHoldReleaseRequestModel))[0]
    assert (req.status, req.decided_by, req.pending_key) == ("approved", "sam", None)

    actions = {a.action for a in await _rows(CaseAuditLogModel,
                                             CaseAuditLogModel.case_id == world["held"].id)}
    assert {"legal_hold_release_requested", "legal_hold_release_approved",
            "legal_hold_released"} <= actions


async def test_refusing_and_withdrawing(api, world, groups):
    await _name_group(api, groups["comp"].id)
    hold = await _hold(api, world["held"])

    await _release(api, hold)
    assert (await _reject(api, hold, BOB)).status_code == 403          # not involved
    assert (await _reject(api, hold, SAM, note="short")).status_code in (400, 422)
    r = await _reject(api, hold, SAM)
    assert r.status_code == 200 and r.json()["request"]["status"] == "rejected"
    assert (await _hold_row(hold)).status == "active"
    assert (await _approve(api, hold, CARL)).json()["error"]["code"] == la.CODE_NO_REQUEST

    await _release(api, hold)                                          # ask again
    r = await _reject(api, hold, ALICE, note="Asked by mistake, withdrawing.")
    assert r.json()["request"]["status"] == "withdrawn"
    assert (await _hold_row(hold)).status == "active"


async def test_a_whole_matter_waits_too(api, world, groups):
    await _name_group(api, groups["comp"].id)
    await _hold(api, world["held"])
    await _hold(api, world["free"])
    r = await api.post(f"{HOLDS}/matters/release", headers=ALICE, json={
        "matter_ref": "Claim 2026/114", "reason": REASON})
    assert r.status_code == 200, r.text
    assert (r.json()["released"], r.json()["pending_approval"]) == (0, 2)
    assert {h.status for h in await _rows(LegalHoldModel)} == {"active"}


# ── the rule binds the people it is for ──────────────────────────────────────


async def test_only_a_member_or_the_superadmin_can_change_a_named_group(api, world, groups):
    assert (await _name_group(api, groups["comp"].id, headers=SAM)).status_code == 403  # no privilege
    assert (await _name_group(api, groups["comp"].id)).status_code == 200

    # alice (admin, not a member) cannot clear it and then release alone
    r = await _name_group(api, None)
    assert r.status_code == 403 and r.json()["error"]["code"] == la.CODE_LOCKED
    assert (await api.get(SETTINGS, headers=ALICE)).json()["can_change"] is False

    # carl, a member with the privilege, can
    assert (await _name_group(api, None, headers=CARL)).status_code == 200

    # and the superadmin can, for a tenant it names
    await _name_group(api, groups["comp"].id)
    acme = world["acme"].id
    assert (await api.put(f"{SETTINGS}?tenant_id={acme}", headers=ALICE,
                          json={"approver_group_id": None})).status_code == 404
    r = await api.put(f"{SETTINGS}?tenant_id={acme}", headers=SUPER,
                      json={"approver_group_id": None})
    assert r.status_code == 200 and r.json()["approver_group_id"] is None
    ev = [e for e in await _rows(SecurityEventModel)
          if e.event_type == "legal_hold.approval_setting_changed"]
    assert any((e.details or {}).get("superadmin_override") for e in ev)


async def test_a_group_must_be_this_tenants_and_active(api, world, groups):
    for g in (groups["theirs"].id, groups["dormant"].id, uuid.uuid4()):
        r = await _name_group(api, g)
        assert r.status_code == 400 and r.json()["error"]["code"] == la.CODE_GROUP, r.text
    names = {g["name"] for g in (await api.get(SETTINGS, headers=ALICE)).json()["groups"]}
    assert "Evil team" not in names and "Compliance" in names


async def test_a_group_switched_off_later_makes_releases_wait(api, world, groups):
    await _name_group(api, groups["comp"].id)
    async with TestSessionFactory() as db:
        (await db.get(AccessGroupModel, groups["comp"].id)).is_active = False
        await db.commit()
    hold = await _hold(api, world["held"])
    assert (await _release(api, hold)).status_code == 202              # still asked
    assert (await _approve(api, hold, SAM)).status_code == 403          # nobody can approve
    s = (await api.get(SETTINGS, headers=ALICE)).json()
    assert s["approver_group_unusable"] is True


async def test_clearing_the_group_lets_a_waiting_hold_be_released_directly(api, world, groups):
    await _name_group(api, groups["comp"].id)
    hold = await _hold(api, world["held"])
    await _release(api, hold)
    await _name_group(api, None, headers=CARL)
    r = await _release(api, hold)
    assert r.status_code == 200 and r.json()["pending"] is False
    req = (await _rows(LegalHoldReleaseRequestModel))[0]
    assert req.status == "superseded" and req.pending_key is None


# ── an install that has not applied 218 ──────────────────────────────────────


async def test_without_the_approval_tables_releases_are_as_before():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from velaris_shared.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    skip = {"legal_hold_settings", "legal_hold_release_requests"}
    tables = [t for t in Base.metadata.sorted_tables if t.name not in skip]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        assert await la.available(db) is False
        assert await la.approver_group_id(db, uuid.uuid4()) is None
        assert await la.pending_for(db, [uuid.uuid4()]) == {}
    await engine.dispose()


# ── found by the 2026-10-15 audit ────────────────────────────────────────────


async def test_two_approvals_at_once_release_once(api, world, groups, monkeypatch):
    """The checks run before the decision; the decision is a conditional UPDATE.
    Simulate the race: the request is decided between one approver's check and
    their write."""
    await _name_group(api, groups["comp"].id)
    hold = await _hold(api, world["held"])
    await _release(api, hold)
    assert (await _approve(api, hold, SAM)).status_code == 200

    stale = (await _rows(LegalHoldReleaseRequestModel))[0]
    stale.status = "pending"                      # what CARL's check saw a moment ago

    async def _stale_pending(session, hold_ids):
        return {stale.hold_id: stale}
    monkeypatch.setattr(la, "pending_for", _stale_pending)
    r = await _approve(api, hold, CARL)
    assert r.status_code == 409, r.text
    events = [e for e in await _rows(SecurityEventModel) if e.event_type == "legal_hold.released"]
    assert len(events) == 1
    req = (await _rows(LegalHoldReleaseRequestModel))[0]
    assert (req.status, req.decided_by) == ("approved", "sam")


async def test_a_matter_release_skips_holds_already_asked_about(api, world, groups):
    await _name_group(api, groups["comp"].id)
    first = await _hold(api, world["held"])
    await _hold(api, world["free"])
    await _release(api, first)                    # one already waiting
    r = await api.post(f"{HOLDS}/matters/release", headers=ALICE, json={
        "matter_ref": "Claim 2026/114", "reason": REASON})
    assert r.status_code == 200, r.text
    assert r.json()["pending_approval"] == 2
    assert len(await _rows(LegalHoldReleaseRequestModel)) == 2


async def test_the_superadmin_cannot_configure_a_tenant_that_does_not_exist(api, world, groups):
    r = await api.put(f"{SETTINGS}?tenant_id={uuid.uuid4()}", headers=SUPER,
                      json={"approver_group_id": None})
    assert r.status_code == 404, r.text


async def test_only_one_decision_can_land_on_a_request(api, world, groups):
    """The lock itself: two sessions each holding the request as `pending` —
    the second conditional UPDATE matches nothing and is refused."""
    from case_service.legal_hold import service as lh

    await _name_group(api, groups["comp"].id)
    hold = await _hold(api, world["held"])
    await _release(api, hold)
    async with TestSessionFactory() as a, TestSessionFactory() as b:
        ra = (await a.execute(select(LegalHoldReleaseRequestModel))).scalars().one()
        rb = (await b.execute(select(LegalHoldReleaseRequestModel))).scalars().one()
        await la._close(a, ra, la.APPROVED, "sam", None)
        await a.commit()
        with pytest.raises(lh.LegalHoldError):
            await la._close(b, rb, la.REJECTED, "carl", "too late to refuse this one")
    req = (await _rows(LegalHoldReleaseRequestModel))[0]
    assert (req.status, req.decided_by) == ("approved", "sam")
