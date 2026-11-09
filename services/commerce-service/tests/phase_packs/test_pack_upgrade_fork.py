"""Accelerator Packs P2 (three-way merge upgrade) and P3 (fork).

The merge matrix is the whole point: an upgrade must never silently overwrite a
tenant's customisation, and must never refuse an upgrade just because something
was customised.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from commerce_service.marketplace import packs


def _bundle(ct_def=None, form_def=None, version="1.0.0"):
    return {
        "case_types": [{"id": "ct-1", "name": "Claim", "version": "1.0",
                        "definition_json": ct_def or {"stages": [{"id": "a", "name": "A", "transitions": []}]}}],
        "form_definitions": [{"id": "fm-1", "name": "Claim Form", "version": "1.0",
                              "definition_json": form_def or {"fields": [{"key": "x", "type": "text"}]}}],
    }


def _pack_bytes(bundle, version="1.0.0"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack.json", json.dumps({"id": "acme/p", "name": "P",
                                             "vertical": "claims", "version": version}))
        for k, v in bundle.items():
            zf.writestr(f"{k}.json", json.dumps(v))
    return buf.getvalue()


# ── the merge matrix ──────────────────────────────────────────────────────────

def test_pack_changed_tenant_untouched_applies():
    base = {"case_types": {"Claim": {"v": 1}}, "form_definitions": {}, "rule_definitions": {}}
    incoming = {"case_types": [{"name": "Claim", "definition_json": {"v": 2}}]}
    current = {"case_types": {"Claim": {"v": 1}}}          # tenant left it alone
    plan = packs.merge_plan(base, incoming, current)
    assert plan["apply"]["case_types"] == ["Claim"]
    assert not plan["conflicts"]


def test_tenant_changed_pack_didnt_keeps_the_tenant_edit():
    base = {"case_types": {"Claim": {"v": 1}}, "form_definitions": {}, "rule_definitions": {}}
    incoming = {"case_types": [{"name": "Claim", "definition_json": {"v": 1}}]}   # unchanged
    current = {"case_types": {"Claim": {"v": 99}}}         # tenant customised
    plan = packs.merge_plan(base, incoming, current)
    assert plan["apply"]["case_types"] == []
    assert not plan["conflicts"]
    assert plan["skipped"][0]["name"] == "Claim"


def test_both_changed_is_a_conflict_and_is_not_applied():
    """The case the snapshot exists for. Auto-resolving loses somebody's work."""
    base = {"case_types": {"Claim": {"v": 1}}, "form_definitions": {}, "rule_definitions": {}}
    incoming = {"case_types": [{"name": "Claim", "definition_json": {"v": 2}}]}
    current = {"case_types": {"Claim": {"v": 99}}}
    plan = packs.merge_plan(base, incoming, current)
    assert plan["apply"]["case_types"] == []
    assert len(plan["conflicts"]) == 1
    assert plan["conflicts"][0]["name"] == "Claim"


def test_new_artifact_in_the_new_version_applies():
    base = {"case_types": {}, "form_definitions": {}, "rule_definitions": {}}
    incoming = {"case_types": [{"name": "Brand New", "definition_json": {"v": 1}}]}
    plan = packs.merge_plan(base, incoming, {"case_types": {}})
    assert plan["apply"]["case_types"] == ["Brand New"]


def test_nothing_changed_is_a_no_op():
    base = {"case_types": {"Claim": {"v": 1}}, "form_definitions": {}, "rule_definitions": {}}
    incoming = {"case_types": [{"name": "Claim", "definition_json": {"v": 1}}]}
    plan = packs.merge_plan(base, incoming, {"case_types": {"Claim": {"v": 1}}})
    assert plan["apply_count"] == 0 and not plan["conflicts"]


def test_one_conflict_does_not_block_the_rest():
    """Refusing a whole upgrade over one customised form makes packs un-upgradable."""
    base = {"case_types": {"Claim": {"v": 1}}, "form_definitions": {"F": {"v": 1}},
            "rule_definitions": {}}
    incoming = {"case_types": [{"name": "Claim", "definition_json": {"v": 2}}],
                "form_definitions": [{"name": "F", "definition_json": {"v": 2}}]}
    current = {"case_types": {"Claim": {"v": 1}}, "form_definitions": {"F": {"v": 99}}}
    plan = packs.merge_plan(base, incoming, current)
    assert plan["apply"]["case_types"] == ["Claim"]        # still applies
    assert len(plan["conflicts"]) == 1                      # form flagged


def test_snapshot_captures_only_the_compared_field():
    snap = packs.snapshot_of(_bundle())
    assert snap["case_types"]["Claim"] == {"stages": [{"id": "a", "name": "A", "transitions": []}]}
    assert "rule_definitions" in snap


def test_filter_bundle_keeps_structural_sections_whole():
    """Stages belong to a case type — importing one without them breaks the lifecycle."""
    bundle = dict(_bundle())
    bundle["stages"] = [{"id": "s1", "case_type_id": "ct-1", "stage_id": "a", "name": "A"}]
    plan = {"apply": {"case_types": ["Claim"], "form_definitions": [], "rule_definitions": []}}
    out = packs._filter_bundle(bundle, plan)
    assert len(out["case_types"]) == 1
    assert "form_definitions" not in out
    assert out["stages"] == bundle["stages"]


# ── upgrade end-to-end ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upgrade_requires_a_provisioned_pack(session):
    with pytest.raises(packs.PackError, match="not provisioned"):
        await packs.upgrade(session, tenant_id="tU", package_id="acme/p",
                            pack=packs.read_pack(_pack_bytes(_bundle())),
                            upgraded_by="dev")


@pytest.mark.asyncio
async def test_upgrade_applies_and_advances_the_version(session):
    await packs.provision(session, tenant_id="tU1", package_id="acme/p",
                          pack=packs.read_pack(_pack_bytes(_bundle())), provisioned_by="dev")
    v2 = _bundle(ct_def={"stages": [{"id": "a", "name": "A2", "transitions": []}]})
    result = await packs.upgrade(session, tenant_id="tU1", package_id="acme/p",
                                 pack=packs.read_pack(_pack_bytes(v2, "2.0.0")),
                                 upgraded_by="dev")
    assert result["status"] == "applied"
    install = await packs.already_provisioned(session, "tU1", "acme/p")
    assert install.pack_version == "2.0.0"


@pytest.mark.asyncio
async def test_upgrade_preview_changes_nothing(session):
    await packs.provision(session, tenant_id="tU2", package_id="acme/p",
                          pack=packs.read_pack(_pack_bytes(_bundle())), provisioned_by="dev")
    before = (await packs.already_provisioned(session, "tU2", "acme/p")).pack_version
    pv = await packs.upgrade_preview(session, tenant_id="tU2", package_id="acme/p",
                                     pack=packs.read_pack(_pack_bytes(_bundle(), "3.0.0")))
    assert pv["to_version"] == "3.0.0"
    after = (await packs.already_provisioned(session, "tU2", "acme/p")).pack_version
    assert before == after


@pytest.mark.asyncio
async def test_upgrade_records_history(session):
    await packs.provision(session, tenant_id="tU3", package_id="acme/p",
                          pack=packs.read_pack(_pack_bytes(_bundle())), provisioned_by="dev")
    v2 = _bundle(ct_def={"stages": [{"id": "b", "name": "B", "transitions": []}]})
    await packs.upgrade(session, tenant_id="tU3", package_id="acme/p",
                        pack=packs.read_pack(_pack_bytes(v2, "2.0.0")), upgraded_by="dev")
    from sqlalchemy import select
    from velaris_shared.db.models import MarketplacePackUpgradeModel as U
    rows = (await session.execute(select(U).where(U.tenant_id == "tU3"))).scalars().all()
    assert len(rows) == 1 and rows[0].to_version == "2.0.0"


# ── P3 fork ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fork_requires_a_provisioned_pack(session):
    with pytest.raises(packs.PackError, match="not provisioned"):
        await packs.fork(session, tenant_id="tF", package_id="acme/p",
                         new_id="me/mine", new_name="Mine")


@pytest.mark.asyncio
async def test_fork_rejects_a_malformed_id(session):
    await packs.provision(session, tenant_id="tF1", package_id="acme/p",
                          pack=packs.read_pack(_pack_bytes(_bundle())), provisioned_by="dev")
    with pytest.raises(packs.PackError, match="publisher"):
        await packs.fork(session, tenant_id="tF1", package_id="acme/p",
                         new_id="nopublisher", new_name="Mine")


@pytest.mark.asyncio
async def test_fork_exports_current_state_and_severs_the_parent(session):
    await packs.provision(session, tenant_id="tF2", package_id="acme/p",
                          pack=packs.read_pack(_pack_bytes(_bundle())), provisioned_by="dev")
    forked = await packs.fork(session, tenant_id="tF2", package_id="acme/p",
                              new_id="me/mine", new_name="My Claims")
    assert forked["meta"]["id"] == "me/mine"
    assert forked["meta"]["forked_from"]["package_id"] == "acme/p"
    assert forked["bundle"]["case_types"][0]["name"] == "Claim"


@pytest.mark.asyncio
async def test_fork_packages_as_community_tier(session):
    """Official tier is derived from the publisher org — never inherited."""
    await packs.provision(session, tenant_id="tF3", package_id="acme/p",
                          pack=packs.read_pack(_pack_bytes(_bundle())), provisioned_by="dev")
    forked = await packs.fork(session, tenant_id="tF3", package_id="acme/p",
                              new_id="me/mine", new_name="Mine")
    blob = packs.fork_to_hxapp(forked)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["publisher_tier"] == "community"
    assert manifest["type"] == packs.PACK_TYPE


@pytest.mark.asyncio
async def test_a_fork_is_a_readable_pack(session):
    """Round-trip: what fork emits must install like any other pack."""
    await packs.provision(session, tenant_id="tF4", package_id="acme/p",
                          pack=packs.read_pack(_pack_bytes(_bundle())), provisioned_by="dev")
    forked = await packs.fork(session, tenant_id="tF4", package_id="acme/p",
                              new_id="me/mine", new_name="Mine")
    reread = packs.read_pack(packs.fork_to_hxapp(forked))
    assert packs.preview(reread)["total_items"] > 0
