"""#27 — Official-tier trust model + marketplace app uninstall lifecycle.

Tier = (source URL org in official_orgs) AND (id in baked official registry).
Uninstall = revoke (keep data) | revoke + delete data (run app teardown).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from case_service.api.routers import marketplace as mkt
from case_service.db.models import (
    MarketplaceInstallModel, TestSuiteModel, TestRunModel, TestResultModel,
)
from case_service.marketplace.official_registry import official_package_ids


OFFICIAL = "https://raw.githubusercontent.com/Velaris-App-OS/Marketplace/main/official/hxtest/velaris.json"
COMMUNITY = "https://raw.githubusercontent.com/Velaris-App-OS/Marketplace/main/community/bob/source.json"
FOREIGN = "https://raw.githubusercontent.com/randomdev/evil/main/sources.json"


# ── Tier trust model (pure) ───────────────────────────────────────────────────

def test_registry_lists_hxtest():
    assert "velaris/hxtest" in official_package_ids()


def test_official_requires_org_folder_and_registry():
    # official folder + official org + registered id → official
    assert mkt._effective_tier(OFFICIAL, "velaris/hxtest") == "official"
    # official folder + official org but id NOT in registry → community
    assert mkt._effective_tier(OFFICIAL, "bob/app") == "community"
    # registered id but FOREIGN org → community (spoof blocked)
    assert mkt._effective_tier(FOREIGN, "velaris/hxtest") == "community"
    # SAME org + registered id but COMMUNITY folder → community (folder boundary)
    assert mkt._effective_tier(COMMUNITY, "velaris/hxtest") == "community"
    # community folder, unlisted id → community
    assert mkt._effective_tier(COMMUNITY, "bob/app") == "community"
    # no id → fail-closed community
    assert mkt._effective_tier(OFFICIAL, "") == "community"


# ── Uninstall lifecycle ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def installed_with_data(session):
    """A non-revoked velaris/hxtest install + one ai_generated suite (HxTest's own
    data) and one core builtin suite (must survive delete)."""
    inst = MarketplaceInstallModel(
        tenant_id="default", package_id="velaris/hxtest", package_version="1.0.0",
        package_type="module", approved_by="test-admin")
    gen = TestSuiteModel(name="Generated · X", suite_type="generated",
                         source="ai_generated", definition=[], version="1.0.0")
    core = TestSuiteModel(name="Platform Smoke", suite_type="platform",
                          source="builtin", definition=[], version="1.0.0")
    session.add_all([inst, gen, core])
    await session.flush()
    run = TestRunModel(suite_id=gen.id, suite_name="gen", status="passed", tenant_id="default")
    session.add(run)
    await session.flush()
    session.add(TestResultModel(run_id=run.id, test_id="t1", status="passed"))
    await session.commit()
    return {"install_id": str(inst.id), "gen_id": gen.id, "core_id": core.id}


@pytest.mark.asyncio
async def test_revoke_keeps_data(client, session, installed_with_data):
    r = await client.delete(f"/api/v1/marketplace/installs/{installed_with_data['install_id']}")
    assert r.status_code == 200, r.text
    assert r.json()["data_teardown"]["deleted"] is False
    # generated suite still present
    assert await session.get(TestSuiteModel, installed_with_data["gen_id"]) is not None


@pytest.mark.asyncio
async def test_revoke_plus_delete_removes_only_hxtest_data(client, session, installed_with_data):
    r = await client.delete(
        f"/api/v1/marketplace/installs/{installed_with_data['install_id']}?delete_data=true")
    assert r.status_code == 200, r.text
    td = r.json()["data_teardown"]
    assert td["deleted"] is True and td["suites"] == 1
    session.expire_all()
    # ai_generated suite gone; core builtin suite survives
    assert await session.get(TestSuiteModel, installed_with_data["gen_id"]) is None
    assert await session.get(TestSuiteModel, installed_with_data["core_id"]) is not None
    # no orphaned runs/results
    assert (await session.execute(select(TestRunModel))).scalars().first() is None
    assert (await session.execute(select(TestResultModel))).scalars().first() is None
