"""Legal holds — Oct item 13, built 2026-10-11.

The platform had no legal hold: retention deleted or anonymized a closed case
on its clock, a customer's erasure went through, a case could be hidden or
moved to another tenant, and nothing could say "not this one".

Pinned here:

* **who** — three privileges (read / place / release), release the narrowest;
  another tenant's case and another tenant's hold are 404; a case with no
  tenant is the platform operator's to freeze;
* **what a hold is** — one per case and matter, several matters at once, held
  until the last is released, rows never deleted;
* **what it freezes** — one test per line of `legal_hold.service.FROZEN`, each
  proving the refusal while held AND that the same call works once released
  (a refusal that never lifts is a different bug);
* **an install without the table** behaves exactly as before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseEventLogModel,
    CaseInstanceModel,
    CaseTimeEntryModel,
    CaseTypeModel,
    CopilotConversationModel,
    CustomerLinkModel,
    DocumentModel,
    HxCanvasBoardModel,
    GDPRRequestModel,
    LegalHoldModel,
    MarketplaceInstallModel,
    PortalCustomerCaseLinkModel,
    PortalCustomerModel,
    RetentionPolicyModel,
    ScheduledReleaseModel,
    SecurityEventModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.compliance import retention
from case_service.engage import entitlement, erasure, identity
from case_service.legal_hold import service as lh
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

ALICE = _headers("alice", ["admin"])              # acme admin
SAM = _headers("sam", ["security"])               # acme: may place, not release
MONA = _headers("mona", ["manager"])              # acme: reads cases, not holds
VERA = _headers("vera", ["viewer"])               # acme: none of the three
BOB = _headers("bob", ["case_worker"])            # acme: owns the `free` case, no legal privilege
MALLORY = _headers("mallory", ["admin"])          # evil admin
OLGA = _headers("olga", ["admin"])                # default tenant = platform operator
SUPER = _headers("root", ["admin", "superadmin"])

HOLDS = "/api/v1/legal-holds"
OLD = datetime.now(timezone.utc) - timedelta(days=4000)
REASON = "Claim 2026/114 — preserve everything."


@pytest.fixture(autouse=True)
def _fresh_table_probe():
    lh._reset_cache_for_tests()
    yield
    lh._reset_cache_for_tests()


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


def _case(ct, tenant, number, **kw):
    return CaseInstanceModel(
        id=uuid.uuid4(), case_number=number, case_type_id=ct.id, case_type_version="1.0.0",
        status=kw.pop("status", "closed"), priority="medium",
        tenant_id=tenant.id if tenant else None,
        created_by=kw.pop("created_by", "alice"), updated_at=OLD,
        data=kw.pop("data", {"email": "jane@x.test", "amount": 10}), **kw)


@pytest_asyncio.fixture
async def world():
    """acme: `held` (the case the tests freeze) and `free` (the control), both
    closed and old enough for any retention policy; Jane, a portal customer
    linked to `held`. `shared` has no tenant. evil has an admin and one case."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        default = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme",
                           settings={"portal": {"enabled": True}})
        evil = TenantModel(id=uuid.uuid4(), slug="evil", name="Evil")
        db.add_all([default, acme, evil])
        db.add(ScheduledReleaseModel(id=uuid.uuid4(), feature_key=entitlement.FEATURE_KEY,
                                     title="CE", status="released", enabled="true"))
        db.add(MarketplaceInstallModel(id=uuid.uuid4(), tenant_id="acme",
                                       package_id=entitlement.PACKAGE_ID, package_version="1",
                                       package_type="module", approved_by="x"))
        for user, tenant in (("alice", "acme"), ("sam", "acme"), ("mona", "acme"),
                             ("vera", "acme"), ("bob", "acme"), ("mallory", "evil"),
                             ("olga", "default")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=user, tenant_id=tenant))
        ct = CaseTypeModel(id=uuid.uuid4(), name="Hold CT", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={})
        db.add(ct)
        await db.flush()
        held = _case(ct, acme, "ACME-H1", portal_submitter_email="jane@x.test")
        free = _case(ct, acme, "ACME-F1", created_by="bob")
        shared = _case(ct, None, "SHARED-1")
        theirs = _case(ct, evil, "EVIL-1", created_by="mallory")
        db.add_all([held, free, shared, theirs])
        await db.flush()
        for case in (held, free):
            db.add(CaseAuditLogModel(id=uuid.uuid4(), case_id=case.id, action="created",
                                     actor_id=case.created_by, timestamp=OLD))
            db.add(CaseEventLogModel(id=uuid.uuid4(), case_id=case.id, case_type_id=ct.id,
                                     activity="created", activity_type="case", timestamp=OLD))
        doc = DocumentModel(id=uuid.uuid4(), case_id=held.id, filename="evidence.pdf",
                            uploaded_by="alice", tenant_id="acme")
        jane = PortalCustomerModel(id=uuid.uuid4(), tenant_id=acme.id,
                                   primary_email="jane@x.test", display_name="Jane Doe")
        db.add_all([doc, jane])
        await db.flush()
        db.add(PortalCustomerCaseLinkModel(customer_id=jane.id, case_id=held.id))
        await db.commit()
    yield {"acme": acme, "evil": evil, "default": default, "held": held, "free": free,
           "shared": shared, "theirs": theirs, "doc": doc, "jane": jane, "ct": ct}
    tenancy.clear_cache()


async def _place(api, case, headers=ALICE, matter="Claim 2026/114", reason=REASON):
    return await api.post(HOLDS, headers=headers, json={
        "case_ids": [str(case.id)], "matter_ref": matter, "reason": reason})


async def _hold(api, case, **kw) -> str:
    r = await _place(api, case, **kw)
    assert r.status_code == 201 and r.json()["placed"] == 1, r.text
    return r.json()["results"][0]["hold"]["id"]


async def _release(api, hold_id, headers=ALICE, reason="Matter settled 2026-10-11."):
    return await api.post(f"{HOLDS}/{hold_id}/release", headers=headers, json={"reason": reason})


async def _rows(model, *where):
    async with TestSessionFactory() as db:
        return (await db.execute(select(model).where(*where))).scalars().all()


# ── who ──────────────────────────────────────────────────────────────────────


async def test_placing_needs_its_own_privilege(api, world):
    assert (await api.post(HOLDS, json={})).status_code == 401
    for who in (VERA, MONA):
        assert (await _place(api, world["held"], headers=who)).status_code == 403
    assert (await _place(api, world["held"], headers=SAM)).status_code == 201
    assert len(await _rows(LegalHoldModel)) == 1


async def test_releasing_is_narrower_than_placing(api, world):
    hold = await _hold(api, world["held"], headers=SAM)
    for who in (SAM, MONA, VERA):                 # the placer cannot lift it
        assert (await _release(api, hold, headers=who)).status_code == 403
    assert (await api.post(f"{HOLDS}/matters/release", headers=SAM, json={
        "matter_ref": "Claim 2026/114", "reason": REASON})).status_code == 403
    r = await _release(api, hold)
    assert r.status_code == 200 and r.json()["case_still_held"] is False, r.text


async def test_reading_the_matter_needs_the_read_privilege(api, world):
    await _hold(api, world["held"])
    for who in (VERA, MONA, BOB):
        assert (await api.get(HOLDS, headers=who)).status_code == 403
    assert (await api.get(HOLDS)).status_code == 401
    listed = (await api.get(HOLDS, headers=SAM)).json()
    assert [h["matter_ref"] for h in listed["holds"]] == ["Claim 2026/114"]
    assert listed["can_release"] is False                       # security reads, admin lifts
    assert (await api.get(HOLDS, headers=ALICE)).json()["can_release"] is True

    # Whoever can read the case learns THAT it is held — not why. bob owns
    # `free`; he has no relationship to `held`, so that one is not his to ask.
    url = f"{HOLDS}/cases/{world['held'].id}"
    assert (await api.get(url)).status_code == 401
    assert (await api.get(url, headers=BOB)).status_code in (403, 404)
    assert (await api.get(url, headers=VERA)).status_code in (403, 404)
    mine = f"{HOLDS}/cases/{world['free'].id}"
    assert (await api.get(mine, headers=BOB)).json() == {
        "case_id": str(world["free"].id), "held": False, "frozen": [],
        "can_read": False, "can_place": False, "can_release": False}
    await _hold(api, world["free"], matter="Second matter")
    seen = (await api.get(mine, headers=BOB)).json()
    assert seen["held"] is True and seen["frozen"] and "holds" not in seen
    assert "Second matter" not in str(seen) and REASON not in str(seen)
    # A manager reads every case in the tenant, and still not the matter.
    mona = (await api.get(url, headers=MONA)).json()
    assert mona["held"] is True and "holds" not in mona and REASON not in str(mona)
    full = (await api.get(url, headers=ALICE)).json()
    assert full["holds"][0]["reason"] == REASON
    assert (full["can_read"], full["can_place"], full["can_release"]) == (True, True, True)


async def test_another_tenant_cannot_place_see_or_release(api, world):
    hold = await _hold(api, world["held"])

    r = await _place(api, world["held"], headers=MALLORY, matter="Planted")
    assert r.status_code == 404, r.text
    # Mixed with a case of their own: still nothing of acme's is confirmed.
    r = await api.post(HOLDS, headers=MALLORY, json={
        "case_ids": [str(world["theirs"].id), str(world["held"].id)],
        "matter_ref": "Planted", "reason": REASON})
    assert r.status_code == 404, r.text
    assert [h.matter_ref for h in await _rows(LegalHoldModel)] == ["Claim 2026/114"]

    assert (await api.get(HOLDS, headers=MALLORY)).json()["holds"] == []
    assert (await api.get(f"{HOLDS}?case_id={world['held'].id}", headers=MALLORY)).status_code == 404
    assert (await api.get(f"{HOLDS}/cases/{world['held'].id}", headers=MALLORY)).status_code == 404
    assert (await _release(api, hold, headers=MALLORY)).status_code == 404
    assert (await api.post(f"{HOLDS}/matters/release", headers=MALLORY, json={
        "matter_ref": "Claim 2026/114", "reason": REASON})).status_code == 404
    assert (await _rows(LegalHoldModel))[0].status == "active"

    # The platform operator is not acme's staff either; break-glass is.
    assert (await _release(api, hold, headers=OLGA)).status_code == 404
    assert (await _release(api, hold, headers=SUPER)).status_code == 200


async def test_a_case_with_no_tenant_is_the_operators_to_freeze(api, world):
    r = await _place(api, world["shared"])                        # acme's admin
    assert r.status_code == 201 and r.json()["placed"] == 0
    assert r.json()["results"][0]["code"] == lh.CODE_NOTFOUND
    await _hold(api, world["shared"], headers=OLGA)
    row = (await _rows(LegalHoldModel))[0]
    assert row.tenant_id == world["default"].id                   # the placer's tenant
    # acme can read the shared case, so it learns it is frozen — and no more.
    seen = (await api.get(f"{HOLDS}/cases/{world['shared'].id}", headers=ALICE)).json()
    assert seen["held"] is True and seen["holds"] == [] and seen["other_active_holds"] == 1


async def test_bad_requests_are_refused_before_anything_is_written(api, world):
    cid = str(world["held"].id)
    for body in (
        {"case_ids": [cid], "matter_ref": "M", "reason": "too short"},
        {"case_ids": [cid], "matter_ref": "", "reason": REASON},
        {"case_ids": [cid], "matter_ref": "   ", "reason": REASON},
        {"case_ids": [cid], "matter_ref": "M" * 121, "reason": REASON},
        {"case_ids": [], "matter_ref": "M", "reason": REASON},
        {"case_ids": [cid] * 501, "matter_ref": "M", "reason": REASON},
        {"case_ids": ["not-a-uuid"], "matter_ref": "M", "reason": REASON},
        {"case_ids": [cid], "matter_ref": "M", "reason": REASON, "tenant_id": "evil"},
        {"case_ids": [cid], "matter_ref": "M", "reason": REASON, "status": "released"},
    ):
        r = await api.post(HOLDS, headers=ALICE, json=body)
        assert r.status_code in (400, 404, 422), (body, r.status_code, r.text)
    assert await _rows(LegalHoldModel) == []
    hold = await _hold(api, world["held"])
    assert (await _release(api, hold, reason="short")).status_code == 422
    assert (await api.post(f"{HOLDS}/{hold}/release", headers=ALICE,
                           json={"reason": REASON, "released_by": "x"})).status_code == 422
    assert (await api.post(f"{HOLDS}/{uuid.uuid4()}/release", headers=ALICE,
                           json={"reason": REASON})).status_code == 404
    assert (await api.get(f"{HOLDS}?status=deleted", headers=ALICE)).status_code == 422


# ── what a hold is ───────────────────────────────────────────────────────────


async def test_one_active_hold_per_case_until_it_is_released(api, world):
    """Decided 2026-10-11: a held case takes no second hold, whatever the matter."""
    first = await _hold(api, world["held"])
    for matter in ("  claim   2026/114 ", "Regulator inquiry 7"):             # same, and another
        again = await _place(api, world["held"], matter=matter)
        assert again.json()["placed"] == 0, matter
        assert again.json()["results"][0]["code"] == lh.CODE_STATE
    for who in (SAM, SUPER):                                                  # nobody may
        assert (await _place(api, world["held"], headers=who, matter="X")).json()["placed"] == 0
    assert len(await _rows(LegalHoldModel)) == 1

    assert (await _release(api, first)).json()["case_still_held"] is False
    assert (await _release(api, first)).status_code == 409                    # already released
    second = await _hold(api, world["held"], matter="Regulator inquiry 7")    # free again
    assert (await _release(api, second)).json()["case_still_held"] is False

    rows = await _rows(LegalHoldModel)
    assert len(rows) == 2 and {r.status for r in rows} == {"released"}        # never deleted
    assert all(r.active_key is None and r.released_by == "alice" for r in rows)


async def test_a_case_holding_two_from_before_the_rule_stays_held_until_both_go(api, world):
    """A box that placed two holds on one case before 2026-10-11 keeps both, and
    the case stays frozen until the last is released."""
    first = await _hold(api, world["held"])
    extra = uuid.uuid4()
    async with TestSessionFactory() as db:
        db.add(LegalHoldModel(
            id=extra, tenant_id=world["held"].tenant_id, case_id=world["held"].id,
            case_number=world["held"].case_number, matter_ref="Older matter",
            reason=REASON, status="active", active_key=f"{world['held'].id}:old",
            placed_by="alice", placed_at=datetime.now(timezone.utc)))
        await db.commit()
    assert (await _place(api, world["held"], matter="Third")).json()["placed"] == 0
    assert (await _release(api, first)).json()["case_still_held"] is True
    assert (await _release(api, str(extra))).json()["case_still_held"] is False


async def test_a_matter_spans_cases_and_releases_together(api, world):
    r = await api.post(HOLDS, headers=ALICE, json={
        "case_ids": [str(world["held"].id), str(world["free"].id), str(world["held"].id),
                     str(uuid.uuid4())],
        "matter_ref": "Class action", "reason": REASON})
    body = r.json()
    assert r.status_code == 201 and body["placed"] == 2 and body["requested"] == 3
    assert [i["ok"] for i in body["results"]] == [True, True, False]
    r = await api.post(f"{HOLDS}/matters/release", headers=ALICE,
                       json={"matter_ref": "class action", "reason": REASON})
    assert r.status_code == 404                                   # a matter is spelled as placed
    r = await api.post(f"{HOLDS}/matters/release", headers=ALICE,
                       json={"matter_ref": "Class  action", "reason": REASON})
    assert r.status_code == 200 and r.json()["released"] == 2, r.text
    assert not await lh_held(world["held"].id) and not await lh_held(world["free"].id)


async def lh_held(case_id) -> bool:
    async with TestSessionFactory() as db:
        return await lh.is_held(db, case_id)


async def test_placing_and_releasing_are_on_the_record(api, world):
    hold = await _hold(api, world["held"])
    await _release(api, hold)
    audit = await _rows(CaseAuditLogModel, CaseAuditLogModel.case_id == world["held"].id,
                        CaseAuditLogModel.action.like("legal_hold_%"))
    assert sorted(a.action for a in audit) == ["legal_hold_placed", "legal_hold_released"]
    assert all(a.actor_id == "alice" and a.details["matter_ref"] == "Claim 2026/114"
               for a in audit)
    events = await _rows(SecurityEventModel, SecurityEventModel.event_type.like("legal_hold.%"))
    assert sorted(e.event_type for e in events) == ["legal_hold.placed", "legal_hold.released"]
    # The stated reason names a dispute; it stays on the hold, not in the logs.
    assert REASON not in str([a.details for a in audit] + [e.details for e in events])


# ── what it freezes ──────────────────────────────────────────────────────────


async def _policy(action, resource="resolved_cases"):
    async with TestSessionFactory() as db:
        p = RetentionPolicyModel(id=uuid.uuid4(), name="t", resource_type=resource,
                                 retention_days=30, action=action, enabled=True)
        db.add(p)
        await db.commit()
    return p


async def _run(policy, apply):
    async with TestSessionFactory() as db:
        p = await db.get(RetentionPolicyModel, policy.id)
        out = await retention.apply_policy(db, p, apply=apply)
        await db.commit()
    return out


async def _get(model, pk):
    async with TestSessionFactory() as db:
        return await db.get(model, pk)


async def test_retention_does_not_anonymize_a_held_case(api, world):
    hold = await _hold(api, world["held"])
    policy = await _policy("anonymize")
    dry = await _run(policy, apply=False)
    assert dry["rows"] == 3 and dry["held_by_legal_hold"] == 1        # free, shared, evil's
    await _run(policy, apply=True)
    held, free = await _get(CaseInstanceModel, world["held"].id), await _get(
        CaseInstanceModel, world["free"].id)
    assert held.data == {"email": "jane@x.test", "amount": 10}
    assert held.portal_submitter_email == "jane@x.test" and held.created_by == "alice"
    assert free.data["email"] == "[anonymized]" and free.created_by == "anonymized"

    await _release(api, hold)
    out = await _run(policy, apply=True)
    assert out["rows"] == 1 and out["held_by_legal_hold"] == 0
    assert (await _get(CaseInstanceModel, world["held"].id)).data["email"] == "[anonymized]"


async def test_retention_does_not_delete_a_held_case(api, world):
    await _hold(api, world["held"])
    async with TestSessionFactory() as db:
        counted = await retention._resolved_cases(db, datetime.now(timezone.utc), False)
        assert counted == 3
        free = await retention._not_held(db, CaseInstanceModel.id)
        ids = set((await db.execute(select(CaseInstanceModel.id).where(free))).scalars().all())
    assert world["held"].id not in ids and world["free"].id in ids
    out = await _run(await _policy("delete"), apply=False)
    assert out["rows"] == 3 and out["held_by_legal_hold"] == 1


async def test_retention_keeps_a_held_cases_audit_and_event_history(api, world):
    await _hold(api, world["held"])
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    async with TestSessionFactory() as db:
        assert await retention._audit_log(db, cutoff, True) == 1
        assert await retention._event_log(db, cutoff, True) == 1
        await db.commit()
    for model in (CaseAuditLogModel, CaseEventLogModel):
        left = await _rows(model, model.timestamp < cutoff)
        assert [r.case_id for r in left] == [world["held"].id], model.__name__


async def test_a_held_case_cannot_be_deleted_but_can_be_restored(api, world):
    held, free = world["held"].id, world["free"].id
    assert (await api.post(f"/api/v1/cases/{held}/actions/delete", headers=ALICE,
                           json={"reason": "tidy"})).status_code == 200
    hold = await _hold(api, world["held"])            # a hidden case can still be frozen
    r = await api.post(f"/api/v1/cases/{held}/actions/restore", headers=ALICE, json={})
    assert r.status_code == 200, r.text               # the way back stays open

    r = await api.post(f"/api/v1/cases/{held}/actions/delete", headers=ALICE,
                       json={"reason": "tidy"})
    assert r.status_code == 409 and r.json()["error"]["code"] == lh.CODE_HELD, r.text
    assert (await _get(CaseInstanceModel, held)).is_deleted is False
    # Ordinary work is NOT frozen, and neither is the case beside it.
    assert (await api.post(f"/api/v1/cases/{held}/actions/add_note", headers=ALICE,
                           json={"params": {"body": "still working it"}})).status_code == 200
    assert (await api.post(f"/api/v1/cases/{free}/actions/delete", headers=ALICE,
                           json={"reason": "tidy"})).status_code == 200

    await _release(api, hold)
    assert (await api.post(f"/api/v1/cases/{held}/actions/delete", headers=ALICE,
                           json={"reason": "tidy"})).status_code == 200


async def test_a_bulk_delete_skips_the_held_case_and_says_so(api, world):
    await _hold(api, world["held"])
    r = await api.post("/api/v1/cases/bulk", headers=ALICE, json={
        "action": "delete", "case_ids": [str(world["held"].id), str(world["free"].id)],
        "params": {"reason": "tidy", "confirm_destructive": True}})
    if r.status_code == 404:
        pytest.skip("no bulk action route at this path")
    assert r.status_code == 200, r.text
    by_id = {i["case_id"]: i for i in r.json()["results"]}
    assert by_id[str(world["held"].id)]["code"] == lh.CODE_HELD
    assert by_id[str(world["free"].id)]["ok"] is True


async def test_a_document_on_a_held_case_cannot_be_deleted(api, world):
    await _hold(api, world["held"])
    r = await api.delete(f"/api/v1/documents/{world['doc'].id}", headers=ALICE)
    assert r.status_code == 409 and r.json()["error"]["code"] == lh.CODE_HELD, r.text
    assert (await _get(DocumentModel, world["doc"].id)).is_deleted is False


async def test_what_is_attached_to_a_held_case_cannot_be_deleted(api, world):
    """A document version, a time entry, an AI conversation, a canvas board.

    Each is seeded on BOTH cases: the one on `free` must still delete, or the
    refusal proves nothing about the hold.
    """
    rows = {}
    async with TestSessionFactory() as db:
        for name, case in (("held", world["held"]), ("free", world["free"])):
            doc = world["doc"] if name == "held" else DocumentModel(
                id=uuid.uuid4(), case_id=case.id, filename="f.pdf", uploaded_by="alice",
                tenant_id="acme")
            entry = CaseTimeEntryModel(id=uuid.uuid4(), tenant_id="acme", case_id=case.id,
                                       user_id="alice", source="timesheet")
            conv = CopilotConversationModel(id=uuid.uuid4(), user_id="alice", case_id=case.id,
                                            tenant_id="acme")
            board = HxCanvasBoardModel(id=uuid.uuid4(), tenant_id="acme", name="b",
                                       case_id=case.id, created_by="alice")
            if name == "free":
                db.add(doc)
            db.add_all([entry, conv, board])
            rows[name] = {
                f"/api/v1/documents/{doc.id}/versions/1": None,
                f"/api/v1/costing/time-entries/{entry.id}": (CaseTimeEntryModel, entry.id),
                f"/api/v1/hxnexus/conversations/{conv.id}": (CopilotConversationModel, conv.id),
                f"/api/v1/hxcanvas/boards/{board.id}": (HxCanvasBoardModel, board.id),
            }
        await db.commit()
    await _hold(api, world["held"])

    for url, row in rows["held"].items():
        r = await api.delete(url, headers=ALICE)
        assert r.status_code == 409 and r.json()["error"]["code"] == lh.CODE_HELD, (url, r.text)
        if row:
            assert await _get(*row) is not None, url
    for url, row in rows["free"].items():
        r = await api.delete(url, headers=ALICE)
        # (the lone version answers its own 409, "delete the document instead")
        assert lh.CODE_HELD not in r.text, (url, r.text)
        if row:
            assert r.status_code == 204 and await _get(*row) is None, (url, r.text)


async def test_a_customer_with_a_held_case_is_not_erased_by_staff(api, world):
    hold = await _hold(api, world["held"])
    url = f"/api/v1/portal/acme/customers/{world['jane'].id}"
    r = await api.delete(url, headers=ALICE)
    assert r.status_code == 409 and r.json()["error"]["code"] == lh.CODE_HELD, r.text
    assert (await _get(PortalCustomerModel, world["jane"].id)).primary_email == "jane@x.test"

    await _release(api, hold)
    assert (await api.delete(url, headers=ALICE)).status_code == 200
    assert (await _get(PortalCustomerModel, world["jane"].id)).display_name == "Deleted User"


async def test_self_erasure_is_refused_without_saying_why_and_is_not_lost(api, world):
    from portal_service.api.routers import portal_customers as pc

    await _hold(api, world["held"])
    app.dependency_overrides[pc._require_customer] = lambda: world["jane"]
    for _ in range(2):
        r = await api.delete("/api/v1/portal/acme/account")
        assert r.status_code == 409, r.text
        assert "legal" not in r.text.lower() and "hold" not in r.text.lower(), r.text
    assert (await _get(PortalCustomerModel, world["jane"].id)).primary_email == "jane@x.test"
    requests = await _rows(GDPRRequestModel, GDPRRequestModel.request_type == "erasure")
    assert len(requests) == 1 and requests[0].status == "pending"       # once, not per click
    assert requests[0].tenant_id == world["acme"].id
    refused = await _rows(SecurityEventModel,
                          SecurityEventModel.event_type == "gdpr.erasure_refused_legal_hold")
    assert len(refused) == 2


async def test_a_rep_desk_profile_with_a_held_case_is_not_erased(api, world):
    acme = world["acme"].id
    async with TestSessionFactory() as db:
        p = await identity.create_profile(db, tenant_uuid=acme, display_name="Jane D")
        db.add(CustomerLinkModel(id=uuid.uuid4(), tenant_id=acme, customer_id=p.id,
                                 object_kind="case", object_id=world["held"].id))
        await db.commit()
    hold = await _hold(api, world["held"])
    url = f"/api/v1/engage/profiles/{p.id}/erase"
    r = await api.post(url, headers=ALICE, json={"reason": "customer asked"})
    assert r.status_code == 409 and r.json()["error"]["code"] == lh.CODE_HELD, r.text
    # Every erasure path calls `forget`; the refusal lives there, by any identifier.
    async with TestSessionFactory() as db:
        for who in ({"emails": ["JANE@x.test"]}, {"profile_ids": [p.id]},
                    {"portal_customer_ids": [str(world["jane"].id)]}):
            with pytest.raises(lh.LegalHoldError):
                await erasure.forget(db, tenant_uuid=acme, reason="t", **who)
        # Somebody else, and the same address in another tenant, are untouched.
        await erasure.forget(db, tenant_uuid=acme, emails=["bob@x.test"], reason="t")
        await erasure.forget(db, tenant_uuid=world["evil"].id, emails=["jane@x.test"], reason="t")
    await _release(api, hold)
    r = await api.post(url, headers=ALICE, json={"reason": "customer asked"})
    assert r.status_code == 200 and r.json()["profiles_anonymized"] == 1, r.text


async def test_a_staff_user_on_a_held_case_is_not_anonymized(api, world):
    hold = await _hold(api, world["held"])
    r = await api.post("/api/v1/enterprise/gdpr/anonymize/alice", headers=ALICE)
    assert r.status_code == 409 and r.json()["error"]["code"] == lh.CODE_HELD, r.text
    assert (await _get(CaseInstanceModel, world["held"].id)).created_by == "alice"
    assert await _rows(GDPRRequestModel) == []                    # nothing half-done
    # bob is only on the free case.
    assert (await api.post("/api/v1/enterprise/gdpr/anonymize/bob", headers=ALICE)
            ).status_code == 200
    await _release(api, hold)
    assert (await api.post("/api/v1/enterprise/gdpr/anonymize/alice", headers=ALICE)
            ).status_code == 200


async def test_a_held_case_cannot_move_to_another_tenant(api, world):
    url = (f"/api/v1/cases/{world['held'].id}/tenant-transfers/preflight"
           "?destination_tenant=evil")
    before = (await api.get(url, headers=ALICE)).json()
    assert "it is under a legal hold" not in before["blockers"]
    await _hold(api, world["held"])
    after = (await api.get(url, headers=ALICE)).json()
    assert after["can_move"] is False and "it is under a legal hold" in after["blockers"]


async def test_a_tenant_with_a_held_case_is_not_deleted_and_its_key_not_shredded(api, world):
    from case_service.hxvault import keyring

    hold = await _hold(api, world["held"])
    url = f"/api/v1/tenants/{world['acme'].id}/permanent"
    r = await api.delete(url, headers=SUPER)
    assert r.status_code == 409 and r.json()["error"]["code"] == lh.CODE_HELD, r.text
    assert await _get(TenantModel, world["acme"].id) is not None
    async with TestSessionFactory() as db:
        with pytest.raises(lh.LegalHoldError):
            await keyring.shred(db, world["acme"].id)
        assert await keyring.shred(db, world["evil"].id) is False     # no key, and no hold
    await _release(api, hold, headers=SUPER)
    async with TestSessionFactory() as db:
        assert await keyring.shred(db, world["acme"].id) is False


async def test_every_frozen_line_is_shown_to_the_operator(api, world):
    await _hold(api, world["held"])
    seen = (await api.get(f"{HOLDS}/cases/{world['held'].id}", headers=ALICE)).json()
    assert seen["frozen"] == list(lh.FROZEN) and len(lh.FROZEN) == 7


# ── an install that has not applied migration 215 ────────────────────────────


async def test_without_the_table_nothing_is_held_and_nothing_breaks():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from velaris_shared.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [t for t in Base.metadata.sorted_tables if t.name != "legal_holds"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        ct = CaseTypeModel(id=uuid.uuid4(), name="Old CT", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={})
        db.add(ct)
        await db.flush()
        case = _case(ct, None, "OLD-1")
        db.add(case)
        await db.commit()

        assert await lh.available(db) is False
        assert await lh.is_held(db, case.id) is False
        assert await lh.not_held(db, CaseInstanceModel.id) is None
        assert await lh.tenant_active_count(db, uuid.uuid4()) == 0
        assert await lh.user_held_cases(db, "alice") == 0
        await lh.require_not_held(db, case.id, "deleted")
        await lh.require_customer_erasable(db, tenant="default", emails=["jane@x.test"])
        # Retention runs exactly as it did, on the same session, after the probe.
        assert await retention._resolved_cases(db, datetime.now(timezone.utc), False) == 1
        assert await retention._anonymize_cases(db, datetime.now(timezone.utc), True) == 1
        assert await retention._held_due(db, datetime.now(timezone.utc), None, None) == 0
        await db.commit()
        assert (await db.get(CaseInstanceModel, case.id)).data["email"] == "[anonymized]"
        assert (await db.execute(select(func.count()).select_from(CaseInstanceModel))
                ).scalar() == 1
    await engine.dispose()


async def test_a_database_error_that_is_not_a_missing_table_is_not_read_as_free():
    from sqlalchemy.exc import OperationalError

    assert lh._is_missing_table(Exception('relation "legal_holds" does not exist'))
    assert lh._is_missing_table(Exception("Table 'velaris.legal_holds' doesn't exist"))
    assert lh._is_missing_table(Exception("no such table: legal_holds"))
    assert not lh._is_missing_table(Exception("connection refused"))
    assert not lh._is_missing_table(Exception('relation "cases" does not exist'))

    class Broken:
        def begin_nested(self):
            raise OperationalError("select", {}, Exception("server closed the connection"))

    with pytest.raises(OperationalError):
        await lh.available(Broken())
