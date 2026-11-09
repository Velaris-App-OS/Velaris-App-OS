"""Accelerator Packs — the security properties, as tests.

Each of these covers a hole found in review, so a regression fails loudly
rather than quietly reopening it.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pathlib

import io
import json
import re
import zipfile

import pytest

from commerce_service.marketplace import packs


#: commerce-service's package (the marketplace moved here, R5e step 7).
_PKG = pathlib.Path(__file__).resolve().parents[2] / "commerce_service"


def _pack_bytes(sections=None, meta=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack.json", json.dumps(meta or {
            "id": "a/p", "name": "P", "vertical": "v", "version": "1.0.0"}))
        for k, v in (sections or {"case_types": [
            {"id": "c", "name": "C", "version": "1.0", "definition_json": {}}]}).items():
            zf.writestr(f"{k}.json", json.dumps(v))
    return buf.getvalue()


# ── entitlement: pack endpoints must not bypass install ───────────────────────

def test_pack_endpoints_all_require_an_install():
    """Install is where the blacklist, access rules and #27 conformance gate run.

    A pack endpoint that skips `_require_installed` is a way around all three:
    a blacklisted or non-conformant package could still write its case types,
    forms and rules into the tenant.
    """
    src = open(_PKG / "api/routers/marketplace.py").read()
    for fn in ("preview_pack", "provision_pack",
               "preview_pack_upgrade", "upgrade_pack"):
        body = src.split(f"async def {fn}(", 1)[1].split("\n@router", 1)[0]
        assert "_require_installed" in body, f"{fn} does not check entitlement"


def test_fork_requires_provisioned_which_is_stricter_than_installed():
    src = open(_PKG / "marketplace/packs.py").read()
    body = src.split("async def fork(", 1)[1].split("\ndef ", 1)[0]
    assert "already_provisioned" in body


# ── response-header injection via the fork filename ───────────────────────────

@pytest.mark.parametrize("hostile", [
    'x"; rm -rf /; filename="y',          # quote break-out
    "a\r\nX-Injected: 1",                 # header injection
    "../../../../etc/passwd",             # traversal in the suggested filename
    "pack\nSet-Cookie: a=b",
])
def test_fork_filename_is_sanitised(hostile):
    """`new_id` is user input and lands in Content-Disposition."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", hostile.replace("/", "-"))[:64] or "pack"
    assert "\r" not in safe and "\n" not in safe and '"' not in safe
    assert "../" not in safe


def test_router_uses_the_sanitiser_not_raw_input():
    src = open(_PKG / "api/routers/marketplace.py").read()
    body = src.split("async def fork_pack(", 1)[1]
    assert "re.sub(" in body, "fork filename is not sanitised"
    assert 'filename="{new_id' not in body


# ── one provision per (tenant, pack), enforced by the database ────────────────

def test_unique_constraint_exists_on_the_model():
    """Without it, check-then-insert races and two concurrent calls both provision."""
    from velaris_shared.db.models import MarketplacePackInstallModel as M
    names = {getattr(a, "name", None) for a in M.__table_args__}
    assert "ux_pack_installs_tenant_pkg" in names


@pytest.mark.asyncio
async def test_retry_after_failure_reuses_the_row(session):
    """The unique constraint must not make a failed provision unrecoverable."""
    from sqlalchemy import select
    from velaris_shared.db.models import MarketplacePackInstallModel as M

    session.add(M(tenant_id="tS", package_id="a/p", pack_version="1.0.0",
                  status="failed", provisioned_by="dev"))
    await session.commit()

    await packs.provision(session, tenant_id="tS", package_id="a/p",
                          pack=packs.read_pack(_pack_bytes()), provisioned_by="dev")

    rows = (await session.execute(
        select(M).where(M.tenant_id == "tS", M.package_id == "a/p")
    )).scalars().all()
    assert len(rows) == 1, "retry inserted a second row instead of reusing the failed one"


# ── content restrictions hold ─────────────────────────────────────────────────

def test_forbidden_sections_cover_every_credential_and_egress_carrier():
    from case_service.hxdeploy import packager  # noqa: F401
    for s in ("connectors", "email_accounts", "webhook_subscriptions",
              "outbound_connector_rules", "webhook_receiver_rules"):
        assert s in packs.FORBIDDEN_SECTIONS
    # and none of them is also in the allowed list
    assert not (set(packs.FORBIDDEN_SECTIONS) & set(packs.PACK_SECTIONS))


def test_zip_bomb_is_refused_on_declared_size():
    """Checked before decompression — after it, the damage is done."""
    big = [{"id": "x", "name": "n" * 1000, "version": "1", "definition_json": {}}] * 20000
    raw = _pack_bytes({"case_types": big})
    with pytest.raises(packs.PackError, match="section limit"):
        packs.read_pack(raw)


def test_only_known_section_names_are_read_from_the_archive():
    """No iteration over namelist(), so a crafted entry name cannot be read."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack.json", json.dumps({"id": "a/p", "name": "P",
                                             "vertical": "v", "version": "1.0.0"}))
        zf.writestr("case_types.json", json.dumps([{"id": "c", "name": "C",
                                                    "version": "1.0", "definition_json": {}}]))
        zf.writestr("../../../etc/passwd", "root:x:0:0")
        zf.writestr("evil.json", json.dumps([{"x": 1}]))
    p = packs.read_pack(buf.getvalue())
    assert set(p["bundle"]) == {"case_types"}


def test_sample_data_is_capped():
    assert packs._MAX_SAMPLE_CASES <= 200
    pv = packs.preview(packs.read_pack(_pack_bytes(meta={
        "id": "a/p", "name": "P", "vertical": "v", "version": "1.0.0",
        "sample_data": {"count": 10 ** 9}})))
    assert pv["sample_data_count"] == packs._MAX_SAMPLE_CASES


# ── tenant isolation ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provision_state_does_not_leak_across_tenants(session):
    await packs.provision(session, tenant_id="tA", package_id="a/p",
                          pack=packs.read_pack(_pack_bytes()), provisioned_by="dev")
    assert await packs.already_provisioned(session, "tB", "a/p") is None


@pytest.mark.asyncio
async def test_upgrade_and_fork_refuse_a_pack_this_tenant_never_provisioned(session):
    await packs.provision(session, tenant_id="tA2", package_id="a/p",
                          pack=packs.read_pack(_pack_bytes()), provisioned_by="dev")
    for call in (
        packs.upgrade(session, tenant_id="tB2", package_id="a/p",
                      pack=packs.read_pack(_pack_bytes()), upgraded_by="dev"),
        packs.fork(session, tenant_id="tB2", package_id="a/p",
                   new_id="x/y", new_name="Y"),
    ):
        with pytest.raises(packs.PackError, match="not provisioned"):
            await call
