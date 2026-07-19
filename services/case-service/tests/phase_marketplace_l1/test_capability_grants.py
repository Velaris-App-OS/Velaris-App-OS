"""Marketplace Layer-1 — capability grants (execution & trust model, mig 122).

Pins: manifest `execution` validation (layer 3 forbidden, layer 2 not yet
installable, descriptor must exist / no traversal / domains required),
install provisions an INERT connector (enabled=False, no credentials) + a
pending grant carrying exactly the requested set, idempotent per
(tenant, package); approval activates the ADMIN-TICKED SUBSET only (superset
400, empty 400) and flips the connector on; revocation is instant inert;
runtime enforcement (host_allowed semantics, block-all before grant, allowed
+ blocked calls land in marketplace_network_log with grant_id); admin-only +
tenant 404 anti-oracle on every grant endpoint; workspace approval links the
production install but never activates capabilities.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from case_service.db.models import (
    ConnectorRegistryModel,
    MarketplaceCapabilityGrantModel,
    MarketplaceInstallModel,
    MarketplaceNetworkLogModel,
    MarketplacePackageCacheModel,
    MarketplaceWorkspaceModel,
)
from case_service.marketplace import grants as mkt_grants
from case_service.marketplace.checksum import ManifestError, parse_and_validate_manifest

MKT = "/api/v1/marketplace"

OPENAPI_SPEC = json.dumps({
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com"}],
    "paths": {"/v1/things": {"post": {"operationId": "createThing", "summary": "Create"}}},
})


def _bundle(manifest: dict, files: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, content in (files or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _manifest(**over) -> dict:
    m = {
        "id": "acme/remote-app", "name": "Acme Remote", "type": "connector",
        "publisher": "acme", "publisher_tier": "community",
        "outbound_domains": ["api.example.com"], "version": "1.0.0",
        "execution": {"layer": 1, "descriptor": "openapi.json",
                      "descriptor_format": "openapi"},
        "scopes": ["cases.read"],
    }
    m.update(over)
    return m


def _layer1_bundle(**over) -> bytes:
    return _bundle(_manifest(**over), {"openapi.json": OPENAPI_SPEC})


async def _seed_package(session, bundle: bytes, package_id="acme/remote-app") -> str:
    session.add(MarketplacePackageCacheModel(
        id=package_id, name="Acme Remote", description="remote app",
        package_type="connector", category="Integration", publisher="acme",
        publisher_tier="community", version="1.0.0", price="free",
        download_url="https://pkg.example.com/app.hxapp",
        checksum_sha256=hashlib.sha256(bundle).hexdigest(),
    ))
    await session.commit()
    return package_id


async def _seed_workspace(session, tenant_id="default", created_by="test-admin") -> MarketplaceWorkspaceModel:
    ws = MarketplaceWorkspaceModel(
        tenant_id=tenant_id, name=f"ws-{uuid.uuid4().hex[:6]}", status="active",
        created_by=created_by,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(ws)
    await session.commit()
    await session.refresh(ws)
    return ws


class _FakeResp:
    def __init__(self, content: bytes):
        self.content = content
    def raise_for_status(self):
        return None


def _patched_download(bundle: bytes):
    return patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_FakeResp(bundle)))


async def _install(client, session, bundle: bytes, package_id="acme/remote-app"):
    await _seed_package(session, bundle, package_id)
    ws = await _seed_workspace(session)
    with _patched_download(bundle):
        r = await client.post(f"{MKT}/workspaces/{ws.id}/install",
                              json={"package_id": package_id})
    return ws, r


class TestManifestExecution:
    def test_layer1_valid_attaches_descriptor_hash(self):
        m = parse_and_validate_manifest(_layer1_bundle())
        assert m["_descriptor_sha256"] == hashlib.sha256(OPENAPI_SPEC.encode()).hexdigest()
        assert m["_descriptor_text"] == OPENAPI_SPEC

    def test_layer3_forbidden(self):
        try:
            parse_and_validate_manifest(_bundle(_manifest(
                execution={"layer": 3, "descriptor": "x", "descriptor_format": "openapi"}), {"x": "y"}))
            raise AssertionError("layer 3 accepted")
        except ManifestError as e:
            assert "forbidden" in str(e)

    def test_layer2_requires_image_declaration(self):
        # Layer 2 is installable (test_layer2.py) but never without an image.
        try:
            parse_and_validate_manifest(_bundle(_manifest(
                execution={"layer": 2, "descriptor": "x", "descriptor_format": "openapi"}), {"x": "y"}))
            raise AssertionError("layer 2 without image accepted")
        except ManifestError as e:
            assert "execution.image" in str(e)

    def test_descriptor_must_exist(self):
        try:
            parse_and_validate_manifest(_bundle(_manifest()))
            raise AssertionError("missing descriptor accepted")
        except ManifestError as e:
            assert "not found in the bundle" in str(e)

    def test_descriptor_traversal_rejected(self):
        try:
            parse_and_validate_manifest(_bundle(_manifest(
                execution={"layer": 1, "descriptor": "../../etc/passwd",
                           "descriptor_format": "openapi"})))
            raise AssertionError("traversal accepted")
        except ManifestError as e:
            assert "relative path" in str(e)

    def test_layer1_requires_outbound_domains(self):
        try:
            parse_and_validate_manifest(_bundle(_manifest(outbound_domains=[]),
                                                {"openapi.json": OPENAPI_SPEC}))
            raise AssertionError("empty domains accepted")
        except ManifestError as e:
            assert "outbound_domain" in str(e)

    def test_declarative_packages_unchanged(self):
        m = _manifest()
        del m["execution"]
        parsed = parse_and_validate_manifest(_bundle(m))
        assert "_descriptor_sha256" not in parsed


class TestInertInstall:
    async def test_install_creates_inert_connector_and_pending_grant(self, client, session):
        ws, r = await _install(client, session, _layer1_bundle())
        assert r.status_code == 200, r.text

        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        assert grant.status == "pending"
        assert grant.granted == {}
        assert grant.requested == {"outbound_domains": ["api.example.com"],
                                   "scopes": ["cases.read"]}
        assert grant.descriptor_sha256 == hashlib.sha256(OPENAPI_SPEC.encode()).hexdigest()
        assert grant.workspace_id == ws.id

        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        assert connector.enabled is False                      # INERT
        assert connector.credentials == {}                     # nothing shipped
        assert connector.name == "mkt:acme/remote-app"
        mkt = connector.config["_marketplace"]
        assert mkt["grant_id"] == str(grant.id)
        assert "granted_domains" not in mkt                    # block-all until approval
        assert connector.config["url"].startswith("https://api.example.com")

    async def test_second_install_reuses_grant(self, client, session):
        bundle = _layer1_bundle()
        _, r1 = await _install(client, session, bundle)
        assert r1.status_code == 200, r1.text
        ws2 = await _seed_workspace(session)
        with _patched_download(bundle):
            r2 = await client.post(f"{MKT}/workspaces/{ws2.id}/install",
                                   json={"package_id": "acme/remote-app"})
        assert r2.status_code == 200, r2.text
        grants = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().all()
        connectors = (await session.execute(select(ConnectorRegistryModel).where(
            ConnectorRegistryModel.name.like("mkt:%")))).scalars().all()
        assert len(grants) == 1 and len(connectors) == 1

    async def test_unmappable_descriptor_400(self, client, session):
        bad_spec = json.dumps({"openapi": "3.0.0", "paths": {}})
        bundle = _bundle(_manifest(), {"openapi.json": bad_spec})
        _, r = await _install(client, session, bundle)
        assert r.status_code == 400
        assert "provisioning failed" in r.json()["detail"]

    async def test_connector_template_descriptor(self, client, session):
        template = json.dumps({"method": "POST", "url": "https://api.example.com/hook",
                               "auth_type": "bearer"})
        bundle = _bundle(_manifest(execution={
            "layer": 1, "descriptor": "template.json",
            "descriptor_format": "connector_template"}), {"template.json": template})
        _, r = await _install(client, session, bundle)
        assert r.status_code == 200, r.text
        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        assert connector.config["url"] == "https://api.example.com/hook"
        assert connector.config["auth_type"] == "bearer"


class TestGrantApproval:
    async def _pending(self, client, session):
        await _install(client, session, _layer1_bundle(
            outbound_domains=["api.example.com", "cdn.example.com"]))
        return (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()

    async def test_approve_subset_activates_connector(self, client, session):
        grant = await self._pending(client, session)
        r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
            "outbound_domains": ["api.example.com"], "scopes": [], "note": "api only"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "granted"
        assert body["granted"] == {"outbound_domains": ["api.example.com"], "scopes": []}

        await session.refresh(grant)
        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        await session.refresh(connector)
        assert connector.enabled is True
        assert connector.config["_marketplace"]["granted_domains"] == ["api.example.com"]

    async def test_superset_rejected_400(self, client, session):
        grant = await self._pending(client, session)
        r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
            "outbound_domains": ["api.example.com", "evil.example.org"], "scopes": []})
        assert r.status_code == 400
        assert "not requested" in r.json()["detail"]

    async def test_empty_domains_rejected_400(self, client, session):
        grant = await self._pending(client, session)
        r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
            "outbound_domains": [], "scopes": []})
        assert r.status_code == 400

    async def test_revoke_is_instant_inert(self, client, session):
        grant = await self._pending(client, session)
        await client.post(f"{MKT}/grants/{grant.id}/approve", json={
            "outbound_domains": ["api.example.com"], "scopes": []})
        r = await client.post(f"{MKT}/grants/{grant.id}/revoke", json={"note": "incident"})
        assert r.status_code == 200
        assert r.json()["status"] == "revoked"

        await session.refresh(grant)
        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        await session.refresh(connector)
        assert connector.enabled is False
        assert "granted_domains" not in connector.config["_marketplace"]

        # revoked is terminal for this grant
        r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
            "outbound_domains": ["api.example.com"], "scopes": []})
        assert r.status_code == 409

    async def test_grant_endpoints_are_tenant_scoped_404(self, client, session):
        from case_service.auth.jwt_handler import create_dev_token
        from case_service.config import get_settings
        from case_service.db.models import UserDirectoryModel
        grant = await self._pending(client, session)
        s = get_settings()
        grant_id = str(grant.id)   # capture before expire_all (MissingGreenlet gotcha)
        other_id = str(uuid.uuid4())
        other = create_dev_token(
            user_id=other_id, username="other-admin", roles=["admin"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
        # tenant_id is resolved from user_directory, not the JWT claim.
        # Write-verify-retry: a request teardown on the shared StaticPool conn
        # can eat the committed directory row (established repo pattern).
        r = None
        for _ in range(3):
            session.expire_all()
            existing = (await session.execute(
                select(UserDirectoryModel).where(UserDirectoryModel.user_id == other_id)
            )).scalars().first()
            if existing is None:
                session.add(UserDirectoryModel(user_id=other_id, tenant_id="other-tenant"))
                await session.commit()
            r = await client.get(f"{MKT}/grants/{grant_id}",
                                 headers={"Authorization": f"Bearer {other}"})
            if r.status_code == 404:
                break
        assert r.status_code == 404

    async def test_grant_endpoints_admin_only(self, client, session):
        from case_service.auth.jwt_handler import create_dev_token
        from case_service.config import get_settings
        grant = await self._pending(client, session)
        s = get_settings()
        dev = create_dev_token(
            user_id=str(uuid.uuid4()), username="dev", roles=["developer"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
        r = await client.post(f"{MKT}/grants/{grant.id}/approve",
                              json={"outbound_domains": ["api.example.com"], "scopes": []},
                              headers={"Authorization": f"Bearer {dev}"})
        assert r.status_code == 403

    async def test_list_grants_filters_status(self, client, session):
        grant = await self._pending(client, session)
        pending = (await client.get(f"{MKT}/grants?status=pending")).json()["grants"]
        assert len(pending) == 1
        await client.post(f"{MKT}/grants/{grant.id}/approve", json={
            "outbound_domains": ["api.example.com"], "scopes": []})
        assert (await client.get(f"{MKT}/grants?status=pending")).json()["grants"] == []
        assert len((await client.get(f"{MKT}/grants?status=granted")).json()["grants"]) == 1


class TestWorkspaceApprovalLinksInstall:
    async def test_approve_workspace_links_install_but_never_activates(self, client, session):
        ws, _ = await _install(client, session, _layer1_bundle())
        ws.status = "submitted"
        ws.submitted_at = datetime.now(timezone.utc)
        await session.commit()
        r = await client.post(f"{MKT}/review-queue/{ws.id}/approve", json={})
        assert r.status_code == 200, r.text

        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        install = (await session.execute(select(MarketplaceInstallModel))).scalars().one()
        assert grant.install_id == install.id
        assert grant.status == "pending"                      # approval of the WORKSPACE
        connector = await session.get(ConnectorRegistryModel, grant.connector_id)
        assert connector.enabled is False                     # is not a capability grant


class TestRuntimeEnforcement:
    def test_host_allowed_semantics(self):
        granted = ["api.example.com"]
        assert mkt_grants.host_allowed("api.example.com", granted)
        assert mkt_grants.host_allowed("sub.api.example.com", granted)
        assert not mkt_grants.host_allowed("evilapi.example.com", granted)
        assert not mkt_grants.host_allowed("api.example.com.evil.org", granted)
        assert not mkt_grants.host_allowed("example.com", granted)
        assert not mkt_grants.host_allowed("", granted)

    async def test_unactivated_connector_blocks_everything(self, client, session):
        from case_service.hxbridge.connectors.http_custom_connector import HttpCustomConnector
        c = HttpCustomConnector({
            "method": "POST", "url": "https://api.example.com/v1/things",
            "_marketplace": {"package_id": "acme/remote-app", "grant_id": None},
        }, {})
        blocked = []
        for _ in range(3):   # write-verify-retry (StaticPool teardown gotcha)
            try:
                await c.execute({})
                raise AssertionError("inert connector executed")
            except RuntimeError as e:
                assert "not activated" in str(e)
            session.expire_all()
            blocked = (await session.execute(select(MarketplaceNetworkLogModel))).scalars().all()
            if blocked:
                break
        assert len(blocked) >= 1 and blocked[0].status == "blocked"

    async def test_ungranted_host_blocked_and_logged(self, client, session):
        from case_service.hxbridge.connectors.http_custom_connector import HttpCustomConnector
        await _install(client, session, _layer1_bundle())
        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        c = HttpCustomConnector({
            "method": "POST", "url": "https://other.example.org/exfil",
            "_marketplace": {"package_id": "acme/remote-app", "grant_id": str(grant.id),
                             "granted_domains": ["api.example.com"]},
        }, {})
        grant_id = grant.id
        row = None
        for _ in range(3):   # write-verify-retry (StaticPool teardown gotcha)
            try:
                await c.execute({})
                raise AssertionError("ungranted host executed")
            except RuntimeError as e:
                assert "not in the granted" in str(e)
            session.expire_all()
            row = (await session.execute(select(MarketplaceNetworkLogModel))).scalars().first()
            if row is not None:
                break
        assert row is not None
        assert row.status == "blocked"
        assert row.grant_id == grant_id
        assert row.is_declared is False                       # undeclared = violation flag

    async def test_granted_call_allowed_and_logged(self, client, session):
        from case_service.hxbridge.connectors.http_custom_connector import HttpCustomConnector
        await _install(client, session, _layer1_bundle())
        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        c = HttpCustomConnector({
            "method": "POST", "url": "https://api.example.com/v1/things",
            "_marketplace": {"package_id": "acme/remote-app", "grant_id": str(grant.id),
                             "granted_domains": ["api.example.com"]},
        }, {})

        class _Resp:
            status_code = 200
            text = "{}"
            def json(self):
                return {"ok": True}

        grant_id = grant.id        # capture before expire_all (MissingGreenlet gotcha)
        # Write-verify-retry (shared StaticPool conn: a request teardown can
        # eat a committed write — established test-side pattern in this repo).
        row = None
        for _ in range(3):
            with patch("case_service.hxbridge.security.validate_outbound_url"), \
                 patch("httpx.AsyncClient.request", new=AsyncMock(return_value=_Resp())):
                out = await c.execute({})
            assert out["status_code"] == 200
            session.expire_all()
            row = (await session.execute(
                select(MarketplaceNetworkLogModel)
                .where(MarketplaceNetworkLogModel.status == "allowed")
            )).scalars().first()
            if row is not None:
                break
        assert row is not None
        assert row.status == "allowed"
        assert row.grant_id == grant_id
        assert row.http_status_code == 200

    async def test_ssrf_guard_runs_on_granted_domain(self, client, session):
        """A granted domain that resolves somewhere internal still gets the
        platform SSRF guard — grants never bypass it."""
        from case_service.hxbridge.connectors.http_custom_connector import HttpCustomConnector
        c = HttpCustomConnector({
            "method": "GET", "url": "https://api.example.com/x",
            "_marketplace": {"package_id": "p", "grant_id": None,
                             "granted_domains": ["api.example.com"]},
        }, {})
        with patch("case_service.hxbridge.security.validate_outbound_url",
                   side_effect=ValueError("internal target")):
            try:
                await c.execute({})
                raise AssertionError("SSRF guard bypassed")
            except ValueError as e:
                assert "internal" in str(e)

    def test_user_built_connectors_unaffected(self):
        from case_service.hxbridge.connectors.http_custom_connector import HttpCustomConnector
        c = HttpCustomConnector({"method": "GET", "url": "https://anything.example.com"}, {})
        assert c._marketplace is None                          # no gate installed
