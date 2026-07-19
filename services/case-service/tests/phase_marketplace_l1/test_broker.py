"""Marketplace Layer-2 — the scoped data broker (/api/v1/broker).

Pins: token auth (uniform 404 for missing/garbage/wrong token AND for a
revoked or merely-pending grant — instant revocation, no oracle), tenant
pinning by construction (cross-tenant case = the same 404), scope
enforcement (`cases.read` = all tenant types; `cases.read:<type>` = only
that type; no cases scope = 403 on list, 404 on detail), the case_type
filter, read-only surface, per-grant rate limit 429, and the broker://
rows in the marketplace network log.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import secrets
import uuid

import pytest_asyncio
from sqlalchemy import select

from case_service.db.models import (
    MarketplaceCapabilityGrantModel,
    MarketplaceNetworkLogModel,
)
from case_service.marketplace import grants as mkt_grants
from tests.conftest import create_case, deploy_case_type

BROKER = "/api/v1/broker"


@pytest_asyncio.fixture(autouse=True)
def _reset_broker_rate_limiter():
    from case_service.api.routers import broker
    broker._rate_limiter._windows.clear()
    yield


async def _granted_broker(session, scopes, tenant="default"):
    """A GRANTED container grant with a broker token — returns (grant, raw)."""
    raw = secrets.token_urlsafe(32)
    grant = MarketplaceCapabilityGrantModel(
        tenant_id=tenant, package_id="acme/container-app",
        descriptor_format=mkt_grants.CONTAINER_DESCRIPTOR_FORMAT,
        descriptor_sha256="sha256:" + "ab" * 32,
        requested={"outbound_domains": [], "scopes": scopes},
        granted={"outbound_domains": [], "scopes": scopes,
                 "broker_token_hash": hashlib.sha256(raw.encode()).hexdigest()},
        status="granted", requested_by="user:test-admin",
    )
    session.add(grant)
    await session.commit()
    await session.refresh(grant)
    return grant, raw


def _hdr(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


async def _seeded_case(client, name=None):
    ct = await deploy_case_type(client, name=name or f'Broker CT {uuid.uuid4().hex[:6]}')
    case = await create_case(client, ct['id'], data={"amount": 42})
    return ct, case


class TestBrokerAuth:
    async def test_no_token_404(self, client):
        r = await client.get(f"{BROKER}/cases")
        assert r.status_code == 404

    async def test_garbage_token_404(self, client):
        r = await client.get(f"{BROKER}/cases", headers=_hdr("garbage"))
        assert r.status_code == 404

    async def test_platform_jwt_is_not_a_broker_token(self, client):
        """The default test client's platform JWT must be useless here."""
        r = await client.get(f"{BROKER}/cases")   # client sends its JWT by default
        assert r.status_code == 404

    async def test_pending_grant_token_404(self, client, session):
        grant, raw = await _granted_broker(session, ["cases.read"])
        grant.status = "pending"
        await session.commit()
        r = await client.get(f"{BROKER}/cases", headers=_hdr(raw))
        assert r.status_code == 404

    async def test_revocation_is_instant(self, client, session):
        grant, raw = await _granted_broker(session, ["cases.read"])
        assert (await client.get(f"{BROKER}/cases", headers=_hdr(raw))).status_code == 200
        grant.status = "revoked"
        await session.commit()
        r = await client.get(f"{BROKER}/cases", headers=_hdr(raw))
        assert r.status_code == 404

    async def test_rate_limit_429(self, client, session):
        from case_service.api.routers import broker
        _, raw = await _granted_broker(session, ["cases.read"])
        old = broker._rate_limiter.max_calls
        broker._rate_limiter.max_calls = 3
        try:
            for _ in range(3):
                assert (await client.get(f"{BROKER}/cases", headers=_hdr(raw))).status_code == 200
            r = await client.get(f"{BROKER}/cases", headers=_hdr(raw))
            assert r.status_code == 429
        finally:
            broker._rate_limiter.max_calls = old


class TestBrokerScopes:
    async def test_full_scope_lists_tenant_cases(self, client, session):
        _, raw = await _granted_broker(session, ["cases.read"])
        # Write-verify-retry (StaticPool teardown gotcha): re-seed if the
        # committed case was eaten before the broker read saw it.
        case = ids = None
        for _ in range(3):
            _, case = await _seeded_case(client)
            r = await client.get(f"{BROKER}/cases", headers=_hdr(raw))
            assert r.status_code == 200, r.text
            ids = [c["id"] for c in r.json()["cases"]]
            if case["id"] in ids:
                break
        assert case["id"] in ids
        got = next(c for c in r.json()["cases"] if c["id"] == case["id"])
        assert got["data"]["amount"] == 42

    async def test_qualified_scope_restricts_type(self, client, session):
        ct_a, case_a = await _seeded_case(client, name=f'Broker A {uuid.uuid4().hex[:6]}')
        ct_b, case_b = await _seeded_case(client, name=f'Broker B {uuid.uuid4().hex[:6]}')
        _, raw = await _granted_broker(session, [f"cases.read:{ct_a['name']}"])
        r = await client.get(f"{BROKER}/cases", headers=_hdr(raw))
        ids = [c["id"] for c in r.json()["cases"]]
        assert case_a["id"] in ids
        assert case_b["id"] not in ids

    async def test_no_cases_scope_403(self, client, session):
        _, raw = await _granted_broker(session, ["something.else"])
        r = await client.get(f"{BROKER}/cases", headers=_hdr(raw))
        assert r.status_code == 403

    async def test_case_type_filter_outside_scope_403(self, client, session):
        ct_a, _ = await _seeded_case(client, name=f'Broker A {uuid.uuid4().hex[:6]}')
        ct_b, _ = await _seeded_case(client, name=f'Broker B {uuid.uuid4().hex[:6]}')
        _, raw = await _granted_broker(session, [f"cases.read:{ct_a['name']}"])
        r = await client.get(f"{BROKER}/cases?case_type={ct_b['name']}", headers=_hdr(raw))
        assert r.status_code == 403

    async def test_detail_respects_scope_with_404(self, client, session):
        ct_a, case_a = await _seeded_case(client, name=f'Broker A {uuid.uuid4().hex[:6]}')
        _, case_b = await _seeded_case(client, name=f'Broker B {uuid.uuid4().hex[:6]}')
        _, raw = await _granted_broker(session, [f"cases.read:{ct_a['name']}"])
        assert (await client.get(f"{BROKER}/cases/{case_a['id']}",
                                 headers=_hdr(raw))).status_code == 200
        r = await client.get(f"{BROKER}/cases/{case_b['id']}", headers=_hdr(raw))
        assert r.status_code == 404                       # scope miss ≡ nonexistent

    async def test_cross_tenant_case_is_404(self, client, session):
        _, case = await _seeded_case(client)              # default-tenant case
        _, raw = await _granted_broker(session, ["cases.read"], tenant="other-tenant")
        r = await client.get(f"{BROKER}/cases/{case['id']}", headers=_hdr(raw))
        assert r.status_code == 404
        r = await client.get(f"{BROKER}/cases", headers=_hdr(raw))
        assert r.status_code == 200 and r.json()["cases"] == []


class TestBrokerAudit:
    async def test_calls_land_in_network_log(self, client, session):
        _, case = await _seeded_case(client)
        grant, raw = await _granted_broker(session, ["cases.read"])
        grant_id = grant.id
        rows = []
        for _ in range(3):   # write-verify-retry (StaticPool teardown gotcha)
            assert (await client.get(f"{BROKER}/cases", headers=_hdr(raw))).status_code == 200
            session.expire_all()
            rows = (await session.execute(
                select(MarketplaceNetworkLogModel).where(
                    MarketplaceNetworkLogModel.grant_id == grant_id)
            )).scalars().all()
            if rows:
                break
        assert rows
        assert rows[0].destination_url.startswith("broker://cases")
        assert rows[0].status == "allowed"

    async def test_blocked_calls_logged_too(self, client, session):
        grant, raw = await _granted_broker(session, ["something.else"])
        grant_id = grant.id
        rows = []
        for _ in range(3):
            assert (await client.get(f"{BROKER}/cases", headers=_hdr(raw))).status_code == 403
            session.expire_all()
            rows = (await session.execute(
                select(MarketplaceNetworkLogModel).where(
                    MarketplaceNetworkLogModel.grant_id == grant_id,
                    MarketplaceNetworkLogModel.status == "blocked")
            )).scalars().all()
            if rows:
                break
        assert rows
        assert rows[0].is_declared is False
