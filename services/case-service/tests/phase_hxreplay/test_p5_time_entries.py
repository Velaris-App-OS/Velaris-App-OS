"""HxReplay §11 P2/P3 — manual timers / timesheets + billing export.

Pins: timer lifecycle (one running per user per case), timesheet entries,
billable rollup priced by role rate with honest fully_priced labelling,
case-level authz (404 anti-oracle), own-entries-only delete, and the
costing.rates-gated billing export (per-case cost is commercially sensitive).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from tests.conftest import deploy_case_type, create_case

COSTING = "/api/v1/costing"


def _limited_headers(roles: list[str]) -> dict:
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="time-limited", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
    )
    return {"Authorization": f"Bearer {token}"}


async def _case(client) -> dict:
    ct = await deploy_case_type(client, name=f"Time CT {uuid.uuid4().hex[:6]}")
    return await create_case(client, ct["id"])


async def _set_rate(client, hourly: float = 60.0):
    r = await client.put(f"{COSTING}/rate-card", json={"hourly_rate": hourly, "currency": "USD"})
    assert r.status_code == 200, r.text


class TestTimer:
    async def test_start_stop_computes_duration(self, client):
        case = await _case(client)
        r = await client.post(f"{COSTING}/cases/{case['id']}/timer/start", json={})
        assert r.status_code == 201
        assert r.json()["running"] is True and r.json()["source"] == "timer"
        r2 = await client.post(f"{COSTING}/cases/{case['id']}/timer/stop")
        assert r2.status_code == 200
        out = r2.json()
        assert out["running"] is False and out["ended_at"]
        assert out["duration_seconds"] >= 0

    async def test_double_start_409(self, client):
        case = await _case(client)
        assert (await client.post(f"{COSTING}/cases/{case['id']}/timer/start", json={})).status_code == 201
        assert (await client.post(f"{COSTING}/cases/{case['id']}/timer/start", json={})).status_code == 409

    async def test_stop_without_timer_404(self, client):
        case = await _case(client)
        assert (await client.post(f"{COSTING}/cases/{case['id']}/timer/stop")).status_code == 404

    async def test_missing_case_404(self, client):
        assert (await client.post(
            f"{COSTING}/cases/{uuid.uuid4()}/timer/start", json={})).status_code == 404


class TestTimesheet:
    async def test_entry_and_rollup_priced(self, client):
        await _set_rate(client, 60.0)
        case = await _case(client)
        r = await client.post(f"{COSTING}/cases/{case['id']}/time-entries",
                              json={"duration_seconds": 1800, "note": "review"})
        assert r.status_code == 201
        r2 = await client.post(f"{COSTING}/cases/{case['id']}/time-entries",
                               json={"duration_seconds": 1800, "billable": False})
        assert r2.status_code == 201
        listed = (await client.get(f"{COSTING}/cases/{case['id']}/time-entries")).json()
        assert len(listed["items"]) == 2
        roll = listed["rollup"]
        assert roll["tracked_seconds"] == 3600
        assert roll["billable_seconds"] == 1800          # non-billable excluded
        assert roll["billable_cost"] == 30.0             # 0.5h × $60
        assert roll["fully_priced"] is True

    async def test_unpriced_role_is_labelled_not_guessed(self, client, session):
        # no rate card at all → cost must be null + fully_priced false (honesty)
        from sqlalchemy import delete
        from case_service.db.models import RateCardModel
        await session.execute(delete(RateCardModel))
        await session.commit()
        case = await _case(client)
        r = await client.post(f"{COSTING}/cases/{case['id']}/time-entries",
                              json={"duration_seconds": 600})
        assert r.status_code == 201
        roll = (await client.get(f"{COSTING}/cases/{case['id']}/time-entries")).json()["rollup"]
        assert roll["billable_cost"] is None
        assert roll["fully_priced"] is False

    async def test_running_timer_not_counted(self, client):
        await _set_rate(client)
        case = await _case(client)
        await client.post(f"{COSTING}/cases/{case['id']}/timer/start", json={})
        roll = (await client.get(f"{COSTING}/cases/{case['id']}/time-entries")).json()["rollup"]
        assert roll["tracked_seconds"] == 0

    async def test_delete_own_only(self, client):
        case = await _case(client)
        r = await client.post(f"{COSTING}/cases/{case['id']}/time-entries",
                              json={"duration_seconds": 300})
        eid = r.json()["id"]
        # another authenticated identity cannot delete it (404 anti-oracle)
        other = _limited_headers(["admin"])
        assert (await client.delete(f"{COSTING}/time-entries/{eid}",
                                    headers=other)).status_code == 404
        assert (await client.delete(f"{COSTING}/time-entries/{eid}")).status_code == 204
        listed = (await client.get(f"{COSTING}/cases/{case['id']}/time-entries")).json()
        assert listed["items"] == []


class TestAuthz:
    async def test_anon_401(self, anon_client):
        cid = uuid.uuid4()
        for m, p in [("POST", f"{COSTING}/cases/{cid}/timer/start"),
                     ("POST", f"{COSTING}/cases/{cid}/time-entries"),
                     ("GET",  f"{COSTING}/cases/{cid}/time-entries"),
                     ("GET",  f"{COSTING}/export")]:
            resp = await anon_client.request(m, p, json={"duration_seconds": 60})
            assert resp.status_code == 401, f"{m} {p}"

    async def test_enforce_unrelated_user_404(self, client):
        from case_service.config import get_settings
        case = await _case(client)                       # admin-owned
        hdrs = _limited_headers(["user"])
        get_settings().hxguard_case_enforcement = "enforce"
        try:
            resp = await client.get(f"{COSTING}/cases/{case['id']}/time-entries",
                                    headers=hdrs)
            assert resp.status_code == 404               # anti-oracle
        finally:
            get_settings().hxguard_case_enforcement = "shadow"


class TestBillingExport:
    async def test_export_requires_costing_capability(self, client):
        # per-case cost is commercially sensitive: non-admin denied
        resp = await client.get(f"{COSTING}/export", headers=_limited_headers(["user"]))
        assert resp.status_code == 403

    async def test_export_lines_and_csv(self, client):
        await _set_rate(client, 120.0)
        case = await _case(client)
        await client.post(f"{COSTING}/cases/{case['id']}/time-entries",
                          json={"duration_seconds": 3600})
        out = (await client.get(f"{COSTING}/export")).json()
        line = next(l for l in out["lines"] if l["case_id"] == case["id"])
        assert line["billable_seconds"] == 3600
        assert line["billable_cost"] == 120.0
        assert line["fully_priced"] is True

        csv_resp = await client.get(f"{COSTING}/export?fmt=csv")
        assert csv_resp.headers["content-type"].startswith("text/csv")
        assert case["id"] in csv_resp.text
        assert csv_resp.text.splitlines()[0].startswith("case_id,")

    async def test_export_case_type_filter(self, client):
        await _set_rate(client)
        ct_a = await deploy_case_type(client, name=f"Bill A {uuid.uuid4().hex[:6]}")
        ct_b = await deploy_case_type(client, name=f"Bill B {uuid.uuid4().hex[:6]}")
        case_a = await create_case(client, ct_a["id"])
        case_b = await create_case(client, ct_b["id"])
        for c in (case_a, case_b):
            await client.post(f"{COSTING}/cases/{c['id']}/time-entries",
                              json={"duration_seconds": 60})
        out = (await client.get(f"{COSTING}/export?case_type_id={ct_a['id']}")).json()
        ids = {l["case_id"] for l in out["lines"]}
        assert case_a["id"] in ids and case_b["id"] not in ids
