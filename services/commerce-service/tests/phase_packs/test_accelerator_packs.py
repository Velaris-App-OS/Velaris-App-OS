"""Accelerator Packs P1 — pack format, provisioning, and the re-provision block.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from commerce_service.marketplace import packs

REPO = Path(__file__).resolve().parents[4]


# ── helpers ───────────────────────────────────────────────────────────────────

def _minimal_sections() -> dict:
    return {
        "case_types": [{
            "id": "ct-1", "name": "Test Claim", "version": "1.0",
            "definition_json": {"stages": [{"id": "a", "name": "A", "transitions": []}]},
        }],
        "form_definitions": [{
            "id": "fm-1", "name": "Test Form", "version": "1.0",
            "definition_json": {"fields": [{"key": "x", "type": "text"}]},
        }],
    }


def _pack(sections: dict | None = None, meta: dict | None = None,
          manifest: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest or {
            "id": "acme/pack", "name": "Acme Pack", "version": "1.0.0",
            "type": "accelerator_pack", "publisher": "Acme",
            "publisher_tier": "community", "outbound_domains": [],
        }))
        zf.writestr("pack.json", json.dumps(meta or {
            "id": "acme/pack", "name": "Acme Pack", "vertical": "claims", "version": "1.0.0",
        }))
        for name, payload in (sections if sections is not None else _minimal_sections()).items():
            zf.writestr(f"{name}.json", json.dumps(payload))
    return buf.getvalue()


# ── pack format ───────────────────────────────────────────────────────────────

def test_read_pack_extracts_meta_and_bundle():
    p = packs.read_pack(_pack())
    assert p["meta"]["vertical"] == "claims"
    assert len(p["bundle"]["case_types"]) == 1
    assert len(p["bundle"]["form_definitions"]) == 1


def test_pack_with_no_content_is_refused():
    with pytest.raises(packs.PackError, match="no provisionable content"):
        packs.read_pack(_pack(sections={}))


def test_corrupt_archive_is_refused():
    with pytest.raises(packs.PackError, match="not a valid"):
        packs.read_pack(b"not a zip at all")


def test_section_must_be_a_json_array():
    with pytest.raises(packs.PackError, match="must be a JSON array"):
        packs.read_pack(_pack(sections={"case_types": {"not": "a list"}}))


def test_malformed_section_json_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack.json", json.dumps({"id": "a", "name": "A", "version": "1", "vertical": "v"}))
        zf.writestr("case_types.json", "{not json")
    with pytest.raises(packs.PackError, match="not valid JSON"):
        packs.read_pack(buf.getvalue())


@pytest.mark.parametrize("section", sorted(packs.FORBIDDEN_SECTIONS))
def test_credential_and_egress_sections_are_refused(section):
    """A content pack must not be able to smuggle in credentials or egress."""
    sections = _minimal_sections()
    sections[section] = [{"id": "x", "name": "sneaky"}]
    with pytest.raises(packs.PackError, match="may not ship"):
        packs.read_pack(_pack(sections=sections))


def test_non_conforming_expression_rule_is_refused():
    """apply_bundle does NOT validate expressions; the pack path must.

    Without this a pack could land a rule the /rules API would reject, which
    would then run on the hardened eval fallback instead of the safe evaluator.
    """
    sections = _minimal_sections()
    sections["rule_definitions"] = [{
        "id": "r1", "name": "evil", "version": "1.0", "rule_type": "expression",
        "definition_json": {"expression": "__import__('os').system('id')"},
    }]
    with pytest.raises(packs.PackError, match="rejected by HxSandbox"):
        packs.read_pack(_pack(sections=sections))


def test_conforming_expression_rule_is_accepted():
    sections = _minimal_sections()
    sections["rule_definitions"] = [{
        "id": "r1", "name": "ok", "version": "1.0", "rule_type": "expression",
        "definition_json": {"expression": "amount > 100"},
    }]
    assert len(packs.read_pack(_pack(sections=sections))["bundle"]["rule_definitions"]) == 1


def test_non_expression_rule_types_are_not_grammar_checked():
    """Only expression rule types carry an expression to validate."""
    sections = _minimal_sections()
    sections["rule_definitions"] = [{
        "id": "r1", "name": "assignment", "version": "1.0", "rule_type": "assignment",
        "definition_json": {"strategy": "round_robin"},
    }]
    assert packs.read_pack(_pack(sections=sections))["bundle"]["rule_definitions"]


# ── preview ───────────────────────────────────────────────────────────────────

def test_preview_counts_without_touching_anything():
    pv = packs.preview(packs.read_pack(_pack()))
    assert pv["vertical"] == "claims"
    assert pv["contents"] == {"form_definitions": 1, "case_types": 1}
    assert pv["total_items"] == 2


def test_preview_reports_sample_data_availability():
    p = packs.read_pack(_pack(meta={
        "id": "a", "name": "A", "vertical": "v", "version": "1.0.0",
        "sample_data": {"count": 25},
    }))
    pv = packs.preview(p)
    assert pv["sample_data_available"] and pv["sample_data_count"] == 25


def test_preview_caps_absurd_sample_counts():
    p = packs.read_pack(_pack(meta={
        "id": "a", "name": "A", "vertical": "v", "version": "1.0.0",
        "sample_data": {"count": 999999},
    }))
    assert packs.preview(p)["sample_data_count"] == packs._MAX_SAMPLE_CASES


# ── provisioning + the P1 re-provision block ──────────────────────────────────

@pytest.mark.asyncio
async def test_provision_actually_creates_the_content(session):
    """Asserted on the DB, not just the return value.

    An earlier version of this test accepted status in (provisioned, failed),
    which would have passed even if apply_bundle imported nothing at all.
    """
    from sqlalchemy import select
    from velaris_shared.db.models import CaseTypeModel

    result = await packs.provision(
        session, tenant_id="t1", package_id="acme/pack",
        pack=packs.read_pack(_pack()), provisioned_by="dev1")

    assert result["status"] == "provisioned", result
    assert result["imported"] == 2, result
    assert result["errors"] == []

    ct = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.name == "Test Claim")
    )).scalar_one_or_none()
    assert ct is not None, "case type was reported imported but is not in the database"


@pytest.mark.asyncio
async def test_second_provision_is_refused(session):
    """P1 provisions once — apply_bundle upserts and would clobber tenant edits."""
    first = await packs.provision(
        session, tenant_id="t1", package_id="acme/pack",
        pack=packs.read_pack(_pack()), provisioned_by="dev1")
    assert first["status"] == "provisioned", first

    with pytest.raises(packs.PackError, match="already provisioned"):
        await packs.provision(
            session, tenant_id="t1", package_id="acme/pack",
            pack=packs.read_pack(_pack()), provisioned_by="dev1")


@pytest.mark.asyncio
async def test_a_failed_provision_does_not_block_a_retry(session):
    """Only a SUCCESSFUL provision blocks — otherwise a failure is unrecoverable."""
    from velaris_shared.db.models import MarketplacePackInstallModel
    session.add(MarketplacePackInstallModel(
        tenant_id="t2", package_id="acme/pack", pack_version="1.0.0",
        status="failed", provisioned_by="dev1"))
    await session.commit()
    assert await packs.already_provisioned(session, "t2", "acme/pack") is None


@pytest.mark.asyncio
async def test_provision_is_tenant_scoped(session):
    await packs.provision(session, tenant_id="t1", package_id="acme/pack",
                          pack=packs.read_pack(_pack()), provisioned_by="dev1")
    # A different tenant has provisioned nothing and must not be blocked.
    assert await packs.already_provisioned(session, "other-tenant", "acme/pack") is None


@pytest.mark.asyncio
async def test_sample_data_is_off_by_default(session):
    result = await packs.provision(
        session, tenant_id="t3", package_id="acme/pack",
        pack=packs.read_pack(_pack(meta={
            "id": "a", "name": "A", "vertical": "v", "version": "1.0.0",
            "sample_data": {"count": 10},
        })), provisioned_by="dev1")
    assert result["sample_cases"] == 0        # available, but not created unasked


# ── the shipped official packs ────────────────────────────────────────────────

@pytest.mark.parametrize("pack_dir", ["velaris-claims-accelerator",
                                      "velaris-onboarding-accelerator"])
def test_shipped_pack_content_is_valid(pack_dir):
    """The first-party packs must satisfy the same rules as any third-party one."""
    src = REPO / "packs" / pack_dir
    if not src.exists():
        pytest.skip(f"{src} not present")

    meta = json.loads((src / "pack.json").read_text())
    for field in ("id", "name", "version", "vertical"):
        assert meta.get(field), f"pack.json missing {field}"

    sections = {}
    for f in src.glob("*.json"):
        if f.name == "pack.json":
            continue
        parsed = json.loads(f.read_text())
        assert isinstance(parsed, list), f"{f.name} must be an array"
        sections[f.stem] = parsed

    # Round-trips through the real reader, including the expression check.
    p = packs.read_pack(_pack(sections=sections, meta=meta))
    assert packs.preview(p)["total_items"] > 0


@pytest.mark.parametrize("pack_dir", ["velaris-claims-accelerator",
                                      "velaris-onboarding-accelerator"])
def test_shipped_pack_stages_resolve(pack_dir):
    """Every stage must point at a case type the pack actually ships, and every
    transition target must exist — a dangling stage is a broken process."""
    src = REPO / "packs" / pack_dir
    if not src.exists():
        pytest.skip(f"{src} not present")

    case_types = json.loads((src / "case_types.json").read_text())
    ct_ids = {c["id"] for c in case_types}
    stage_defs = {s["id"] for c in case_types
                  for s in (c["definition_json"].get("stages") or [])}

    for c in case_types:
        for s in (c["definition_json"].get("stages") or []):
            for t in (s.get("transitions") or []):
                assert t in stage_defs, f"{c['name']}: transition to unknown stage {t!r}"

    stages_file = src / "stages.json"
    if stages_file.exists():
        for s in json.loads(stages_file.read_text()):
            assert s["case_type_id"] in ct_ids, f"stage {s['name']} → unknown case type"
            assert s["stage_id"] in stage_defs, f"stage {s['name']} not in the lifecycle"
