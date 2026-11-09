"""Compliance evidence packs crossed tenants — proven and fixed 2026-10-11.

Read from the code on 2026-10-09 and recorded as "not proven with a second
tenant". Proven here first: an `evil` admin generated a pack that named
`acme`'s admin by email and counted acme's audit actions, GDPR requests and
security events; and because a report was stored with NO tenant, the path
guard's rule for `report_id` never fired — evil listed and downloaded the
report acme had generated.

Pinned:
* a tenant's pack is built from that tenant's rows only;
* a report records the tenant that generated it, and another tenant's is 404;
* a report with no tenant (every one stored before this fix, and every one the
  platform operator generates) is install-wide, so it is the operator's alone;
* the platform operator's pack is the whole install, exactly as before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseTypeModel,
    ComplianceReportModel,
    GDPRRequestModel,
    SecurityEventModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

ACME = _headers("alice", ["admin"])
EVIL = _headers("mallory", ["admin"])
OPERATOR = _headers("olga", ["admin"])            # default tenant = platform operator
SUPER = _headers("root", ["admin", "superadmin"])

GENERATE = "/api/v1/compliance/reports/generate"
REPORTS = "/api/v1/compliance/reports"


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    """acme has an admin, a case with 3 audit actions, a GDPR request and a
    security event. evil has an admin and nothing else."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        default = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        evil = TenantModel(id=uuid.uuid4(), slug="evil", name="Evil")
        db.add_all([default, acme, evil])
        db.add_all([
            UserDirectoryModel(id=uuid.uuid4(), user_id="alice", tenant_id="acme",
                               email="alice@acme.test", display_name="Alice Acme",
                               roles=["admin"]),
            UserDirectoryModel(id=uuid.uuid4(), user_id="mallory", tenant_id="evil",
                               email="mallory@evil.test", display_name="Mallory",
                               roles=["admin"]),
            UserDirectoryModel(id=uuid.uuid4(), user_id="olga", tenant_id="default",
                               email="olga@platform.test", display_name="Olga",
                               roles=["admin"]),
        ])
        ct = CaseTypeModel(id=uuid.uuid4(), name="Comp CT", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={})
        db.add(ct)
        await db.flush()
        case = CaseInstanceModel(
            id=uuid.uuid4(), case_number="ACME-C1", case_type_id=ct.id,
            case_type_version="1.0.0", status="new", priority="medium",
            tenant_id=acme.id, created_by="alice", data={})
        db.add(case)
        await db.flush()
        for _ in range(3):
            db.add(CaseAuditLogModel(id=uuid.uuid4(), case_id=case.id,
                                     action="acme_secret_action", actor_id="alice"))
        db.add(GDPRRequestModel(id=uuid.uuid4(), tenant_id=acme.id, subject_id="s1",
                                request_type="erasure", status="pending"))
        db.add(SecurityEventModel(id=uuid.uuid4(), event_type="acme_login_failed",
                                  user_id="alice", outcome="denied"))
        await db.commit()
    yield
    tenancy.clear_cache()


async def _generate(api, headers) -> str:
    r = await api.post(GENERATE, headers=headers, json={"framework": "soc2", "period_days": 7})
    assert r.status_code == 200, r.text
    return r.json()["report_id"]


async def _pack(api, headers, report_id) -> dict:
    r = await api.get(f"{REPORTS}/{report_id}/download?fmt=json", headers=headers)
    assert r.status_code == 200, r.text
    return json.loads(r.content)


async def test_a_tenants_pack_holds_only_that_tenants_rows(api, world):
    rid = await _generate(api, EVIL)
    r = await api.get(f"{REPORTS}/{rid}/download?fmt=json", headers=EVIL)
    assert r.status_code == 200, r.text
    assert "alice" not in r.text and "acme" not in r.text, r.text

    ev = json.loads(r.content)["evidence"]
    assert [u["user_id"] for u in ev["access_review"]["admin_users"]] == ["mallory"]
    assert ev["access_review"]["active_users"] == 1
    assert ev["audit_summary"]["total_actions"] == 0
    assert ev["gdpr_requests"]["total"] == 0
    assert ev["security_events"]["total_events"] == 0
    # The chain is one per install: a tenant is told whether it verifies, and
    # none of the install-wide row counts or break details.
    assert set(ev["audit_integrity"]) == {"control", "verification"}
    assert "breaks" not in ev["audit_integrity"]["verification"]


async def test_the_summary_returned_and_listed_is_scoped_too(api, world):
    r = await api.post(GENERATE, headers=EVIL, json={"framework": "soc2", "period_days": 7})
    s = r.json()["summary"]
    assert (s["active_users"], s["admin_count"], s["audit_actions"],
            s["gdpr_requests"], s["security_events"]) == (1, 1, 0, 0, 0)


async def test_a_tenants_own_pack_still_has_its_own_evidence(api, world):
    ev = (await _pack(api, ACME, await _generate(api, ACME)))["evidence"]
    assert [u["email"] for u in ev["access_review"]["admin_users"]] == ["alice@acme.test"]
    assert ev["audit_summary"]["by_action"] == {"acme_secret_action": 3}
    assert ev["gdpr_requests"]["total"] == 1
    assert ev["security_events"]["by_type"] == {"acme_login_failed": 1}
    assert ev["security_events"]["failed_events"] == 1


async def test_another_tenants_report_is_not_listed_or_downloadable(api, world):
    rid = await _generate(api, ACME)
    async with TestSessionFactory() as db:
        assert (await db.get(ComplianceReportModel, uuid.UUID(rid))).tenant_id == "acme"

    assert rid not in (await api.get(REPORTS, headers=EVIL)).text
    for fmt in ("json", "pdf"):
        r = await api.get(f"{REPORTS}/{rid}/download?fmt={fmt}", headers=EVIL)
        assert r.status_code == 404, (fmt, r.status_code)

    assert rid in (await api.get(REPORTS, headers=ACME)).text
    assert rid in (await api.get(REPORTS, headers=SUPER)).text
    assert (await api.get(f"{REPORTS}/{rid}/download", headers=SUPER)).status_code == 200


async def test_a_report_with_no_tenant_is_the_operators_alone(api, world):
    """Every report stored before the fix has no tenant, and was generated over
    the whole install — its listed summary and its re-generated bytes both."""
    legacy = ComplianceReportModel(
        id=uuid.uuid4(), framework="soc2", cadence="on_demand",
        period_start=datetime.now(timezone.utc) - timedelta(days=7),
        period_end=datetime.now(timezone.utc) + timedelta(minutes=5),
        generated_by="olga", summary={"active_users": 3, "admin_count": 3})
    async with TestSessionFactory() as db:
        db.add(legacy)
        await db.commit()
    rid = str(legacy.id)

    assert rid not in (await api.get(REPORTS, headers=EVIL)).text
    assert (await api.get(f"{REPORTS}/{rid}/download", headers=EVIL)).status_code == 404

    for who in (OPERATOR, SUPER):
        assert rid in (await api.get(REPORTS, headers=who)).text
        ev = (await _pack(api, who, rid))["evidence"]
        assert ev["access_review"]["active_users"] == 3


async def test_the_operators_pack_is_the_whole_install_as_before(api, world):
    rid = await _generate(api, OPERATOR)
    async with TestSessionFactory() as db:
        assert (await db.get(ComplianceReportModel, uuid.UUID(rid))).tenant_id is None

    ev = (await _pack(api, OPERATOR, rid))["evidence"]
    assert ev["access_review"]["active_users"] == 3
    assert ev["audit_summary"]["by_action"] == {"acme_secret_action": 3}
    assert ev["gdpr_requests"]["total"] == 1
    assert ev["security_events"]["total_events"] == 1
    assert {"status", "verification"} <= set(ev["audit_integrity"])
    assert "breaks" in ev["audit_integrity"]["verification"]


async def test_a_caller_with_no_tenant_claim_is_unchanged(api, world):
    """A single-tenant install: its admins have no directory tenant at all."""
    solo = _headers("nobody-in-the-directory", ["admin"])
    rid = await _generate(api, solo)
    assert rid in (await api.get(REPORTS, headers=solo)).text
    ev = (await _pack(api, solo, rid))["evidence"]
    assert ev["access_review"]["active_users"] == 3
    assert ev["audit_summary"]["total_actions"] == 3
