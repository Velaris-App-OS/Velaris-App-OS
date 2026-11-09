"""Oct item 5 — industry action rails, from a marketplace pack or the tenant.

Pinned: a rail config can order, hide, relabel, guide, preset from a closed
list and RAISE a tier — and is refused, with its reason, for anything else
(lowering a tier, adding an action, a customer placeholder, an unknown field);
the rail route applies the right one and a raised tier really blocks; a pack
carries rails through read, provision, upgrade (keeping tenant edits) and fork;
and the three shipped packs pass the same validator the platform uses.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    EngageRailConfigModel,
    MarketplaceInstallModel,
    ScheduledReleaseModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.engage import action_rail, entitlement, identity, interactions, rail_config
from tests.conftest import app  # the in-process gateway: commerce + case-service
from commerce_service.marketplace import packs
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio
REPO = Path(__file__).resolve().parents[3]


def _rail(**over):
    base = {"key": "card-dispute", "name": "Card dispute", "industry": "banking",
            "applies_to_case_types": ["Card Dispute"],
            "actions": [{"key": "document_request", "label": "Request evidence",
                         "min_tier": "full", "guidance": "Receipts please.",
                         "defaults": {"message": "Upload here: {portal_link}"}},
                        {"key": "payment", "hidden": True}]}
    base.update(over)
    return base


# ── the validator ────────────────────────────────────────────────────────────


def test_a_valid_rail_normalises():
    out = rail_config.validate(_rail())
    assert out["actions"][0]["min_tier"] == "full" and out["actions"][1]["hidden"] is True


@pytest.mark.parametrize("bad,why", [
    ({"actions": [{"key": "payment", "min_tier": "partial"}]}, "never lower"),
    ({"actions": [{"key": "consent", "min_tier": "none"}]}, "never lower"),
    ({"actions": [{"key": "wire_money"}]}, "cannot add one"),
    ({"actions": [{"key": "reply", "defaults": {"message": "Hi {first_name}"}}]}, "only placeholder"),
    ({"actions": [{"key": "reply", "defaults": {"message": "Go to {portal_link}"}}]}, "only placeholder"),
    ({"actions": [{"key": "reply", "run": "rm -rf /"}]}, "unknown field"),
    ({"actions": [{"key": "payment", "defaults": {"currency": "btc"}}]}, "currency"),
    ({"actions": [{"key": "callback", "defaults": {"minutes": 7}}]}, "minutes"),
    ({"actions": [{"key": "reply", "label": "x\x07y"}]}, "control"),
    ({"actions": [{"key": "reply"}, {"key": "reply"}]}, "twice"),
    ({"actions": []}, "at least one"),
    ({"key": "Bad Key"}, "key is"),
    ({"privilege": "admin"}, "unknown field"),
])
def test_a_rail_may_not(bad, why):
    with pytest.raises(rail_config.RailError, match=why):
        rail_config.validate(_rail(**bad))


def test_a_pack_ships_at_most_twenty_rails_with_distinct_keys():
    with pytest.raises(rail_config.RailError, match="twice"):
        rail_config.validate_many([_rail(), _rail()])
    with pytest.raises(rail_config.RailError, match="at most"):
        rail_config.validate_many([_rail(key=f"r-{i}") for i in range(21)])


def test_the_tier_floor_is_item_twos_own():
    assert rail_config.BASE_TIER == {s.key: s.min_tier for s in action_rail.SPECS}


def test_apply_orders_hides_relabels_and_presets():
    actions = [{"key": k, "label": k, "enabled": True, "blocked_by": [], "notes": []}
               for k in rail_config.ACTION_KEYS]
    out = rail_config.apply(actions, rail_config.validate(_rail()))
    keys = [a["key"] for a in out]
    assert keys[0] == "document_request" and "payment" not in keys
    assert len(keys) == len(rail_config.ACTION_KEYS) - 1
    assert out[0]["label"] == "Request evidence" and out[0]["guidance"] == "Receipts please."
    assert out[0]["defaults"]["message"] == "Upload here: {portal_link}"


# ── the rail route applies it ────────────────────────────────────────────────


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default",
                        settings={"portal": {"enabled": True}})
        o = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([t, o])
        db.add(ScheduledReleaseModel(id=uuid.uuid4(), feature_key=entitlement.FEATURE_KEY,
                                     title="CE", status="released", enabled="true"))
        for ten in ("default", "other"):
            db.add(MarketplaceInstallModel(id=uuid.uuid4(), tenant_id=ten,
                                           package_id=entitlement.PACKAGE_ID, package_version="1",
                                           package_type="module", approved_by="x"))
        for u, ten in (("ann", "default"), ("rep", "default"), ("mal", "other")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=u, tenant_id=ten))
        ct = CaseTypeModel(id=uuid.uuid4(), name="Card Dispute", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={"stages": []})
        db.add(ct)
        await db.flush()
        jane = await identity.create_profile(db, tenant_uuid=t.id, display_name="Jane")
        case = CaseInstanceModel(id=uuid.uuid4(), case_number="CD-1", case_type_id=ct.id,
                                 case_type_version="1.0.0", status="open", priority="medium",
                                 # The rep's own case: case access (ReBAC) decides what
                                 # the rail lists, and a stranger's case is not listed.
                                 tenant_id=t.id, created_by="rep", data={})
        db.add(case)
        await db.flush()
        await identity.link(db, tenant_uuid=t.id, customer_id=jane.id, object_kind="case",
                            object_id=case.id)
        pop = await interactions.screen_pop(db, tenant_uuid=t.id, tenant_token="default",
                                            channel="voice")
        call = await db.get(__import__("velaris_shared.db.models", fromlist=["x"]).CustomerInteractionModel,
                            uuid.UUID(pop["interaction_id"]))
        call.customer_id = jane.id
        await interactions.verify(db, interaction=call, factor="otp", ok=True, to_tier="partial")
        await db.commit()
        return {"t": t, "o": o, "jane": jane, "call": call.id}


async def _add(world, rail, *, source="pack", enabled=True, tenant=None):
    async with TestSessionFactory() as db:
        cfg = rail_config.validate(rail)
        db.add(EngageRailConfigModel(id=uuid.uuid4(), tenant_id=(tenant or world["t"]).id,
                                     rail_key=cfg["key"], name=cfg["name"], industry=cfg["industry"],
                                     source=source, config=cfg, enabled=enabled))
        await db.commit()


async def _rail_for(world, rail_key=None, roles=("case_worker",)):
    async with TestSessionFactory() as db:
        from velaris_shared.db.models import CustomerInteractionModel
        return await action_rail.rail(
            db, user=AuthenticatedUser(user_id="rep", roles=list(roles), tenant_id="default"),
            tenant_uuid=world["t"].id, tenant_token="default", profile=world["jane"],
            interaction=await db.get(CustomerInteractionModel, world["call"]), rail_key=rail_key)


async def test_no_config_means_the_rail_as_before(world):
    out = await _rail_for(world)
    assert out["rail"] is None and out["available_rails"] == []
    assert [a["key"] for a in out["actions"]] == [s.key for s in action_rail.SPECS]


async def test_the_case_type_picks_the_rail_and_a_raised_tier_blocks(world):
    await _add(world, _rail())
    out = await _rail_for(world)
    assert out["rail"]["key"] == "card-dispute"
    doc = next(a for a in out["actions"] if a["key"] == "document_request")
    assert doc["label"] == "Request evidence"
    # The call is PARTIAL; this rail needs Full for evidence requests.
    assert [b["kind"] for b in doc["blocked_by"]].count("tier") == 1
    assert "payment" not in [a["key"] for a in out["actions"]]


async def test_two_general_rails_are_the_reps_choice_not_the_servers(world):
    await _add(world, _rail(key="a-general", applies_to_case_types=[]))
    await _add(world, _rail(key="b-general", applies_to_case_types=[], name="Other"))
    assert (await _rail_for(world))["rail"] is None
    assert (await _rail_for(world, rail_key="b-general"))["rail"]["key"] == "b-general"


async def test_a_disabled_or_another_tenants_rail_is_never_used(world):
    await _add(world, _rail(), enabled=False)
    await _add(world, _rail(key="theirs"), tenant=world["o"])
    out = await _rail_for(world)
    assert out["rail"] is None
    assert (await _rail_for(world, rail_key="theirs"))["rail"] is None


# ── the routes ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_a_tenant_authors_edits_and_deletes_its_own_rail(api, world):
    admin = _headers("ann", ["admin"])
    r = await api.put("/api/v1/engage/rails/my-rail", headers=admin,
                      json={"config": {"name": "Mine", "actions": [{"key": "reply"}]}})
    assert r.status_code == 200 and r.json()["source"] == "tenant", r.text
    bad = await api.put("/api/v1/engage/rails/my-rail", headers=admin,
                        json={"config": {"name": "Mine", "actions": [{"key": "payment",
                                                                      "min_tier": "none"}]}})
    assert bad.status_code == 422 and "never lower" in bad.text
    assert (await api.put("/api/v1/engage/rails/my-rail", headers=_headers("rep", ["case_worker"]),
                          json={"config": {"name": "x", "actions": [{"key": "reply"}]}})
            ).status_code == 403
    listed = await api.get("/api/v1/engage/rails", headers=_headers("rep", ["case_worker"]))
    assert [x["key"] for x in listed.json()["rails"]] == ["my-rail"]
    assert (await api.delete("/api/v1/engage/rails/my-rail", headers=admin)).json()["deleted"]


async def test_deleting_a_pack_rail_disables_it(api, world):
    await _add(world, _rail())
    r = await api.delete("/api/v1/engage/rails/card-dispute", headers=_headers("ann", ["admin"]))
    assert r.json()["disabled"] is True and r.json()["rail"]["enabled"] is False


async def test_another_tenant_sees_and_touches_none_of_it(api, world):
    await _add(world, _rail())
    mal = _headers("mal", ["admin"])
    assert (await api.get("/api/v1/engage/rails", headers=mal)).json()["rails"] == []
    assert (await api.delete("/api/v1/engage/rails/card-dispute", headers=mal)).status_code == 404


# ── the pack lifecycle ───────────────────────────────────────────────────────


def _hxapp(rails, *, version="1.0.0", sections=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"id": "velaris/test-rail", "name": "T",
                                                 "version": version, "type": "accelerator_pack"}))
        zf.writestr("pack.json", json.dumps({"id": "velaris/test-rail", "name": "T",
                                             "vertical": "banking", "version": version}))
        zf.writestr("engage_rails.json", json.dumps(rails))
        for k, v in (sections or {}).items():
            zf.writestr(f"{k}.json", json.dumps(v))
    return buf.getvalue()


async def test_a_rails_only_pack_reads_and_a_bad_one_is_refused():
    # The rail rules are case-service's (R5e step 7): `read_pack` parses, and
    # `validate_pack_rails` — which its only caller runs next — refuses.
    p = await packs.validate_pack_rails(packs.read_pack(_hxapp([_rail()])))
    assert p["bundle"] == {} and p["rails"][0]["key"] == "card-dispute"
    assert packs.preview(p)["contents"] == {"engage_rails": 1}
    bad = packs.read_pack(_hxapp([_rail(actions=[{"key": "payment", "min_tier": "none"}])]))
    with pytest.raises(packs.PackError, match="never lower"):
        await packs.validate_pack_rails(bad)


async def test_provision_upgrade_and_fork_carry_rails_and_keep_edits(world):
    pid = "velaris/test-rail"
    async with TestSessionFactory() as db:
        out = await packs.provision(db, tenant_id="default", package_id=pid,
                                    pack=packs.read_pack(_hxapp([_rail(), _rail(key="second",
                                                                             name="Second")])),
                                    provisioned_by="ann")
    assert out["status"] == "provisioned" and sorted(out["rails"]["created"]) == ["card-dispute", "second"]
    async with TestSessionFactory() as db:                # the tenant edits one
        row = (await db.execute(select(EngageRailConfigModel).where(
            EngageRailConfigModel.rail_key == "second"))).scalars().one()
        row.edited_at, row.name = row.created_at, "Our wording"
        await db.commit()
    v2 = packs.read_pack(_hxapp([_rail(name="Card dispute v2"), _rail(key="second", name="Second v2")],
                                version="1.1.0"))
    async with TestSessionFactory() as db:
        up = await packs.upgrade(db, tenant_id="default", package_id=pid, pack=v2, upgraded_by="ann")
    assert up["rails"]["updated"] == ["card-dispute"] and up["rails"]["kept_tenant_edits"] == ["second"]
    async with TestSessionFactory() as db:
        names = {r.rail_key: r.name for r in (await db.execute(select(EngageRailConfigModel))).scalars()}
        assert names == {"card-dispute": "Card dispute v2", "second": "Our wording"}
        forked = await packs.fork(db, tenant_id="default", package_id=pid,
                                  new_id="acme/my-rails", new_name="Mine")
    assert {r["key"] for r in forked["rails"]} == {"card-dispute", "second"}
    assert "engage_rails.json" in zipfile.ZipFile(io.BytesIO(packs.fork_to_hxapp(forked))).namelist()


@pytest.mark.parametrize("pack", ["velaris-banking-dispute-rail", "velaris-telco-portout-rail",
                                  "velaris-clinic-booking-rail"])
def test_the_shipped_packs_pass_the_platforms_own_validator(pack):
    rails = json.loads((REPO / "packs" / pack / "engage_rails.json").read_text())
    assert rail_config.validate_many(rails)


async def test_switching_a_pack_rail_off_does_not_freeze_it(api, world):
    """Off is not an edit: a switched-off pack rail must still take upgrades."""
    await _add(world, _rail())
    r = await api.patch("/api/v1/engage/rails/card-dispute", headers=_headers("ann", ["admin"]),
                        json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["edited"] is False
    r = await api.delete("/api/v1/engage/rails/card-dispute", headers=_headers("ann", ["admin"]))
    assert r.json()["rail"]["edited"] is False


@pytest.mark.parametrize("pid", ["velaris/banking-dispute-rail", "velaris/telco-portout-rail",
                                 "velaris/clinic-booking-rail"])
def test_the_published_bundles_pass_the_install_path(pid):
    """What install actually runs on a downloaded bundle: the checksum pinned in
    the official index, the manifest validator, and read_pack (rails validated).
    A bundle that failed any of these would be listed and uninstallable."""
    from velaris_shared.apps.checksum import parse_and_validate_manifest, verify_checksum

    index = json.loads((REPO / "marketplace" / "official" / "sources.json").read_text())
    entry = next(p for p in index["packages"] if p["id"] == pid)
    v = entry["versions"][0]
    rel = v["download_url"].split("/main/", 1)[1]
    blob = (REPO / "marketplace" / rel).read_bytes()
    verify_checksum(blob, v["checksum_sha256"])
    assert parse_and_validate_manifest(blob)["type"] == packs.PACK_TYPE
    assert packs.read_pack(blob)["rails"]
    registry = json.loads((REPO / "services" / "commerce-service" / "commerce_service"
                           / "marketplace" / "official_registry.json").read_text())
    assert pid in registry["official_packages"]


async def test_a_revoked_pack_stops_shaping_the_desk(world):
    """Found in the audit: a pack's rails kept applying after the pack was
    uninstalled or revoked (e.g. blacklisted). A pack rail now needs a live install."""
    pid = "velaris/test-rail"
    async with TestSessionFactory() as db:
        db.add(MarketplaceInstallModel(id=uuid.uuid4(), tenant_id="default", package_id=pid,
                                       package_version="1.0.0", package_type="accelerator_pack",
                                       approved_by="ann"))
        await db.commit()
        await packs.provision(db, tenant_id="default", package_id=pid,
                              pack=packs.read_pack(_hxapp([_rail()])), provisioned_by="ann")
    assert (await _rail_for(world))["rail"]["key"] == "card-dispute"
    async with TestSessionFactory() as db:
        inst = (await db.execute(select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.package_id == pid))).scalars().one()
        from datetime import datetime, timezone
        inst.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    out = await _rail_for(world)
    assert out["rail"] is None and out["available_rails"] == []


async def test_another_pack_cannot_take_over_a_rail_key(world):
    """A second pack shipping the same key must not overwrite the first pack's
    rail, nor the tenant's own — it is kept and reported."""
    async with TestSessionFactory() as db:
        await packs.provision(db, tenant_id="default", package_id="velaris/first",
                              pack=packs.read_pack(_hxapp([_rail()])), provisioned_by="ann")
    async with TestSessionFactory() as db:
        out = await packs.provision(db, tenant_id="default", package_id="evil/second",
                                    pack=packs.read_pack(_hxapp([_rail(name="Hijacked")])),
                                    provisioned_by="ann")
    assert out["rails"]["kept_tenant_edits"] == ["card-dispute"]
    async with TestSessionFactory() as db:
        row = (await db.execute(select(EngageRailConfigModel))).scalars().one()
    assert row.package_id == "velaris/first" and row.name == "Card dispute"
