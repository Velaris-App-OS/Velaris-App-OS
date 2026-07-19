"""Marketplace Layer-1 P2 — templated-connector drift detection (mig 123).

Pins: classification (widening = new domains/scopes; breaking = active-op
shape change / removed ops / removed response fields; additive otherwise),
the tier policy (community NEVER auto-applies — even additive changes are
held; official auto-applies additive only), the hold semantics (proposed
payload stored, status pending_reapproval, the LIVE mapping and granted set
untouched), drift approve (ticked subset of the NEW request, mapping_version
bump, proposed cleared), drift reject (proposed discarded, live mapping never
touched), re-install with a changed descriptor triggers drift instead of a
second connector, and the update-approve hook (fail-closed 502 when the new
bundle can't be verified; drift result in the response).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from case_service.db.models import (
    ConnectorRegistryModel,
    MarketplaceCapabilityGrantModel,
    MarketplacePackageCacheModel,
    MarketplaceUpdateModel,
)
from case_service.marketplace import grants as mkt_grants

from .test_capability_grants import (
    MKT,
    OPENAPI_SPEC,
    _bundle,
    _install,
    _layer1_bundle,
    _manifest,
    _patched_download,
    _seed_workspace,
)

SPEC_V2_ADDITIVE = json.dumps({
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/v1/things": {"post": {"operationId": "createThing", "summary": "Create"}},
        "/v1/things/{id}": {"get": {"operationId": "getThing", "summary": "Read"}},
    },
})

SPEC_V2_BREAKING = json.dumps({
    "openapi": "3.0.0",
    "servers": [{"url": "https://api2.example.com"}],   # active op URL changes
    "paths": {"/v2/items": {"post": {"operationId": "createItem", "summary": "Create"}}},
})


def _cfg(url="https://api.example.com/v1/things", method="POST", ops=None, mapping=None):
    return {"method": method, "url": url, "headers": {}, "auth_type": "none",
            "body_template": "", "response_mapping": mapping or {},
            "operations": ops or []}


class TestClassifyDrift:
    def test_new_domain_is_widening(self):
        cls, reasons = mkt_grants.classify_drift(
            _cfg(), {"outbound_domains": ["api.example.com"], "scopes": []},
            _cfg(), {"outbound_domains": ["api.example.com", "cdn.example.com"], "scopes": []})
        assert cls == "widening"
        assert "cdn.example.com" in reasons[0]

    def test_new_scope_is_widening(self):
        cls, _ = mkt_grants.classify_drift(
            _cfg(), {"outbound_domains": ["a.com"], "scopes": []},
            _cfg(), {"outbound_domains": ["a.com"], "scopes": ["cases.write"]})
        assert cls == "widening"

    def test_url_change_is_breaking(self):
        cls, reasons = mkt_grants.classify_drift(
            _cfg(url="https://a.com/v1"), {"outbound_domains": ["a.com"], "scopes": []},
            _cfg(url="https://a.com/v2"), {"outbound_domains": ["a.com"], "scopes": []})
        assert cls == "breaking"
        assert "URL changed" in reasons[0]

    def test_removed_operation_is_breaking(self):
        cls, _ = mkt_grants.classify_drift(
            _cfg(ops=[{"operation_id": "a"}, {"operation_id": "b"}]),
            {"outbound_domains": ["a.com"], "scopes": []},
            _cfg(ops=[{"operation_id": "a"}]),
            {"outbound_domains": ["a.com"], "scopes": []})
        assert cls == "breaking"

    def test_removed_response_field_is_breaking(self):
        cls, _ = mkt_grants.classify_drift(
            _cfg(mapping={"x": "data.x"}), {"outbound_domains": ["a.com"], "scopes": []},
            _cfg(mapping={}), {"outbound_domains": ["a.com"], "scopes": []})
        assert cls == "breaking"

    def test_new_operation_is_additive(self):
        cls, _ = mkt_grants.classify_drift(
            _cfg(ops=[{"operation_id": "a"}]), {"outbound_domains": ["a.com"], "scopes": []},
            _cfg(ops=[{"operation_id": "a"}, {"operation_id": "b"}]),
            {"outbound_domains": ["a.com"], "scopes": []})
        assert cls == "additive"

    def test_dropped_domain_is_not_widening(self):
        cls, _ = mkt_grants.classify_drift(
            _cfg(), {"outbound_domains": ["a.com", "b.com"], "scopes": []},
            _cfg(), {"outbound_domains": ["a.com"], "scopes": []})
        assert cls == "additive"


async def _granted_setup(client, session):
    """Installed + approved Layer-1 package — the live baseline for drift."""
    await _install(client, session, _layer1_bundle())
    grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
    r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
        "outbound_domains": ["api.example.com"], "scopes": ["cases.read"]})
    assert r.status_code == 200, r.text
    await session.refresh(grant)
    return grant


def _drift_manifest(spec: str, **over) -> dict:
    """A parsed-manifest lookalike with the derived descriptor fields."""
    m = _manifest(version="2.0.0", **over)
    m["_descriptor_text"] = spec
    m["_descriptor_sha256"] = hashlib.sha256(spec.encode()).hexdigest()
    return m


class TestDriftHold:
    async def test_community_holds_even_additive(self, client, session):
        grant = await _granted_setup(client, session)
        old_config = (await session.get(ConnectorRegistryModel, grant.connector_id)).config
        out = await mkt_grants.apply_descriptor_drift(
            session, grant=grant,
            manifest=_drift_manifest(SPEC_V2_ADDITIVE), publisher_tier="community")
        await session.commit()
        assert out["action"] == "pending_reapproval"
        assert out["classification"] == "additive"

        await session.refresh(grant)
        assert grant.status == "pending_reapproval"
        assert grant.proposed["classification"] == "additive"
        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        await session.refresh(connector)
        assert connector.config == old_config              # live mapping untouched
        assert connector.enabled is True                   # keeps running

    async def test_official_auto_applies_additive_only(self, client, session):
        grant = await _granted_setup(client, session)
        out = await mkt_grants.apply_descriptor_drift(
            session, grant=grant,
            manifest=_drift_manifest(SPEC_V2_ADDITIVE), publisher_tier="official")
        await session.commit()
        assert out["action"] == "auto_applied"

        await session.refresh(grant)
        assert grant.status == "granted"                   # never left service
        assert grant.mapping_version == 2
        assert grant.descriptor_sha256 == hashlib.sha256(SPEC_V2_ADDITIVE.encode()).hexdigest()
        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        await session.refresh(connector)
        assert len(connector.config["operations"]) == 2    # new op landed
        # the enforcement block survives the config swap
        assert connector.config["_marketplace"]["granted_domains"] == ["api.example.com"]

    async def test_official_still_holds_widening(self, client, session):
        grant = await _granted_setup(client, session)
        out = await mkt_grants.apply_descriptor_drift(
            session, grant=grant,
            manifest=_drift_manifest(SPEC_V2_ADDITIVE,
                                     outbound_domains=["api.example.com", "cdn.example.com"]),
            publisher_tier="official")
        assert out["action"] == "pending_reapproval"
        assert out["classification"] == "widening"

    async def test_official_still_holds_breaking(self, client, session):
        grant = await _granted_setup(client, session)
        out = await mkt_grants.apply_descriptor_drift(
            session, grant=grant,
            manifest=_drift_manifest(SPEC_V2_BREAKING,
                                     outbound_domains=["api.example.com"]),
            publisher_tier="official")
        assert out["action"] == "pending_reapproval"
        assert out["classification"] == "breaking"

    async def test_unchanged_descriptor_is_noop(self, client, session):
        grant = await _granted_setup(client, session)
        out = await mkt_grants.apply_descriptor_drift(
            session, grant=grant,
            manifest=_drift_manifest(OPENAPI_SPEC), publisher_tier="community")
        assert out["action"] == "unchanged"
        assert grant.status == "granted"

    async def test_reinstall_with_changed_descriptor_triggers_drift(self, client, session):
        await _granted_setup(client, session)
        bundle_v2 = _bundle(_manifest(version="2.0.0"), {"openapi.json": SPEC_V2_ADDITIVE})
        # the poll would have refreshed the cache with the publisher's new checksum
        pkg = await session.get(MarketplacePackageCacheModel, "acme/remote-app")
        pkg.checksum_sha256 = hashlib.sha256(bundle_v2).hexdigest()
        await session.commit()
        ws2 = await _seed_workspace(session)
        with _patched_download(bundle_v2):
            r = await client.post(f"{MKT}/workspaces/{ws2.id}/install",
                                  json={"package_id": "acme/remote-app"})
        assert r.status_code == 200, r.text
        grants = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().all()
        connectors = (await session.execute(select(ConnectorRegistryModel).where(
            ConnectorRegistryModel.name.like("mkt:%")))).scalars().all()
        assert len(grants) == 1 and len(connectors) == 1   # never a second connector
        assert grants[0].status == "pending_reapproval"    # community = held


class TestDriftDecision:
    async def _held(self, client, session, widening=False):
        grant = await _granted_setup(client, session)
        domains = ["api.example.com", "cdn.example.com"] if widening else ["api.example.com"]
        await mkt_grants.apply_descriptor_drift(
            session, grant=grant,
            manifest=_drift_manifest(SPEC_V2_ADDITIVE, outbound_domains=domains),
            publisher_tier="community")
        await session.commit()
        await session.refresh(grant)
        return grant

    async def test_approve_drift_applies_ticked_subset(self, client, session):
        grant = await self._held(client, session, widening=True)
        r = await client.post(f"{MKT}/grants/{grant.id}/drift/approve", json={
            "outbound_domains": ["api.example.com"],       # cdn NOT ticked
            "scopes": ["cases.read"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "granted"
        assert body["proposed"] is None
        assert body["mapping_version"] == 2
        assert body["granted"]["outbound_domains"] == ["api.example.com"]

        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        assert len(connector.config["operations"]) == 2    # new mapping live
        assert connector.config["_marketplace"]["granted_domains"] == ["api.example.com"]

    async def test_approve_drift_superset_400(self, client, session):
        grant = await self._held(client, session)
        r = await client.post(f"{MKT}/grants/{grant.id}/drift/approve", json={
            "outbound_domains": ["api.example.com", "evil.org"], "scopes": []})
        assert r.status_code == 400

    async def test_reject_drift_keeps_live_mapping(self, client, session):
        grant = await self._held(client, session)
        old_config = (await session.get(ConnectorRegistryModel, grant.connector_id)).config
        r = await client.post(f"{MKT}/grants/{grant.id}/drift/reject", json={"note": "no"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "granted"                 # back in service state
        assert body["proposed"] is None
        assert body["mapping_version"] == 1                # never bumped
        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        await session.refresh(connector)
        assert connector.config == old_config

    async def test_drift_decision_without_pending_400(self, client, session):
        grant = await _granted_setup(client, session)
        r = await client.post(f"{MKT}/grants/{grant.id}/drift/reject", json={})
        assert r.status_code == 400


class TestUpdateApproveHook:
    async def _update_row(self, session, fast_track=True):
        # Write-verify-retry: a request teardown on the shared StaticPool conn
        # can eat the committed row (established repo pattern).
        for _ in range(3):
            session.expire_all()
            existing = (await session.execute(
                select(MarketplaceUpdateModel).where(
                    MarketplaceUpdateModel.tenant_id == "default",
                    MarketplaceUpdateModel.package_id == "acme/remote-app",
                )
            )).scalars().first()
            if existing is not None:
                return existing
            session.add(MarketplaceUpdateModel(
                tenant_id="default", package_id="acme/remote-app",
                installed_version="1.0.0", available_version="2.0.0",
                fast_track=fast_track, status="pending",
            ))
            await session.commit()
        raise AssertionError("update row never persisted")

    async def test_update_approve_runs_drift(self, client, session):
        await _granted_setup(client, session)
        bundle_v2 = _bundle(_manifest(version="2.0.0"), {"openapi.json": SPEC_V2_ADDITIVE})
        pkg = await session.get(MarketplacePackageCacheModel, "acme/remote-app")
        pkg.checksum_sha256 = hashlib.sha256(bundle_v2).hexdigest()
        pkg.version = "2.0.0"
        await session.commit()
        upd = await self._update_row(session)
        upd_id = str(upd.id)

        with _patched_download(bundle_v2):
            r = await client.post(f"{MKT}/updates/{upd_id}/approve")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["drift"]["action"] == "pending_reapproval"   # community tier
        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        assert grant.status == "pending_reapproval"

    async def test_update_approve_fails_closed_on_bad_bundle(self, client, session):
        await _granted_setup(client, session)
        upd = await self._update_row(session)
        upd_id = str(upd.id)
        # checksum in the cache doesn't match what the "publisher" now serves
        with _patched_download(b"tampered-bytes"):
            r = await client.post(f"{MKT}/updates/{upd_id}/approve")
        assert r.status_code == 502
        assert "not applied" in r.json()["detail"]
        session.expire_all()
        row = (await session.execute(
            select(MarketplaceUpdateModel).where(
                MarketplaceUpdateModel.id == uuid.UUID(upd_id))
        )).scalars().one()
        assert row.status == "pending"                     # nothing landed
