"""Marketplace Layer-2 — publisher containers (mig 124).

Pins: layer-2 manifest validation (digest mandatory + format, no embedded
digest, port/env shapes, empty outbound_domains allowed — egress-DROP is the
default posture), install declares a container + pending grant WITHOUT
running anything (registry allowlist fail-closed from config, never the
manifest), approval fail-closed ordering (signature policy > digest-pinned
pull > hardened provision, all mocked — a provisioning failure aborts the
approval and records the error), empty-domain grants activate for containers,
revocation stops the container instantly, image-digest drift is ALWAYS held
(new code, both tiers — never additive), drift approval swaps the digest and
restarts, and `registry_of` semantics.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import uuid
from unittest.mock import patch

from sqlalchemy import select

from case_service.config import get_settings
from case_service.db.models import (
    MarketplaceCapabilityGrantModel,
    MarketplaceContainerModel,
)
from case_service.marketplace import grants as mkt_grants
from case_service.marketplace import runtime
from case_service.marketplace.checksum import ManifestError, parse_and_validate_manifest

from .test_capability_grants import MKT, _bundle, _install

DIGEST = "sha256:" + "ab12" * 16
DIGEST2 = "sha256:" + "cd34" * 16


def _l2_manifest(**over) -> dict:
    m = {
        "id": "acme/container-app", "name": "Acme Container", "type": "module",
        "publisher": "acme", "publisher_tier": "community",
        "outbound_domains": [], "version": "1.0.0",
        "execution": {"layer": 2, "image": "ghcr.io/acme/app:1.0",
                      "image_digest": DIGEST},
        "scopes": ["cases.read"],
    }
    m.update(over)
    return m


def _l2_bundle(**over) -> bytes:
    return _bundle(_l2_manifest(**over))


class TestLayer2Manifest:
    def test_valid_layer2(self):
        m = parse_and_validate_manifest(_l2_bundle())
        assert m["execution"]["image_digest"] == DIGEST

    def test_digest_required(self):
        try:
            parse_and_validate_manifest(_bundle(_l2_manifest(
                execution={"layer": 2, "image": "ghcr.io/acme/app:1.0"})))
            raise AssertionError("missing digest accepted")
        except ManifestError as e:
            assert "image_digest" in str(e)

    def test_tag_is_not_an_identity(self):
        try:
            parse_and_validate_manifest(_bundle(_l2_manifest(
                execution={"layer": 2, "image": "ghcr.io/acme/app:1.0",
                           "image_digest": "v1.0.0"})))
            raise AssertionError("tag-as-digest accepted")
        except ManifestError as e:
            assert "sha256" in str(e)

    def test_embedded_digest_rejected(self):
        try:
            parse_and_validate_manifest(_bundle(_l2_manifest(
                execution={"layer": 2, "image": f"ghcr.io/acme/app@{DIGEST}",
                           "image_digest": DIGEST})))
            raise AssertionError("embedded digest accepted")
        except ManifestError as e:
            assert "must not embed" in str(e)

    def test_empty_domains_allowed_for_layer2(self):
        m = parse_and_validate_manifest(_l2_bundle(outbound_domains=[]))
        assert m["outbound_domains"] == []

    def test_bad_port_rejected(self):
        try:
            parse_and_validate_manifest(_bundle(_l2_manifest(
                execution={"layer": 2, "image": "ghcr.io/acme/app:1.0",
                           "image_digest": DIGEST, "port": 99999})))
            raise AssertionError("bad port accepted")
        except ManifestError as e:
            assert "port" in str(e)

    def test_registry_of(self):
        assert runtime.registry_of("nginx") == "docker.io"
        assert runtime.registry_of("library/nginx:1.25") == "docker.io"
        assert runtime.registry_of("ghcr.io/acme/app:1.0") == "ghcr.io"
        assert runtime.registry_of("registry.example.com:5000/x/y") == "registry.example.com"


async def _install_l2(client, session, bundle=None, package_id="acme/container-app"):
    return await _install(client, session, bundle or _l2_bundle(), package_id)


class TestLayer2Install:
    async def test_install_declares_container_nothing_runs(self, client, session):
        _, r = await _install_l2(client, session)
        assert r.status_code == 200, r.text
        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        assert grant.status == "pending"
        assert grant.descriptor_format == "container_image"
        assert grant.descriptor_sha256 == DIGEST
        assert grant.connector_id is None                  # containers, not connectors
        row = (await session.execute(select(MarketplaceContainerModel))).scalars().one()
        assert row.status == "declared"
        assert row.container_id is None                    # NOTHING runs at install
        assert row.image_digest == DIGEST
        assert row.registry == "ghcr.io"

    async def test_disallowed_registry_400(self, client, session):
        bundle = _l2_bundle(execution={
            "layer": 2, "image": "evil-registry.example.com/acme/app:1.0",
            "image_digest": DIGEST})
        _, r = await _install_l2(client, session, bundle)
        assert r.status_code == 400
        assert "not in the platform allowlist" in r.json()["detail"]

    async def test_empty_allowlist_fails_closed(self, client, session):
        s = get_settings()
        old = s.marketplace_l2_registries
        s.marketplace_l2_registries = ""
        try:
            _, r = await _install_l2(client, session)
            assert r.status_code == 400
            assert "install refused" in r.json()["detail"]
        finally:
            s.marketplace_l2_registries = old


class _RuntimeMocks:
    """Approve-path docker mocks: signature > pull > provision."""
    def __init__(self, provision_fail=False):
        self.provision_fail = provision_fail
        self.provisioned: list[dict] = []
        self.stopped: list[str] = []

    def __enter__(self):
        def _provision(**kw):
            if self.provision_fail:
                raise runtime.Layer2Error("image entrypoint crashed")
            self.provisioned.append(kw)
            return "deadbeef" * 8
        self._patches = [
            patch("case_service.marketplace.runtime.verify_image_signature", return_value=False),
            patch("case_service.marketplace.runtime.pull_pinned_image", return_value=None),
            patch("case_service.marketplace.runtime.provision_app_container",
                  side_effect=lambda **kw: _provision(**kw)),
            patch("case_service.marketplace.runtime.stop_app_container",
                  side_effect=lambda cid: self.stopped.append(cid)),
            patch("case_service.marketplace.runtime.container_running", return_value=False),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._patches:
            p.stop()


class TestLayer2GrantLifecycle:
    async def _pending(self, client, session):
        await _install_l2(client, session)
        return (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()

    async def test_approve_starts_hardened_container(self, client, session):
        grant = await self._pending(client, session)
        with _RuntimeMocks() as rt:
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": [], "scopes": ["cases.read"]})   # egress-DROP grant
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "granted"
        assert len(rt.provisioned) == 1
        assert rt.provisioned[0]["digest"] == DIGEST
        assert rt.provisioned[0]["granted_domains"] == []
        row = (await session.execute(select(MarketplaceContainerModel))).scalars().one()
        await session.refresh(row)
        assert row.status == "running"
        assert row.container_id == "deadbeef" * 8
        assert row.pulled_at is not None                   # provenance recorded

    async def test_provision_failure_aborts_approval(self, client, session):
        grant = await self._pending(client, session)
        with _RuntimeMocks(provision_fail=True):
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": [], "scopes": []})
        assert r.status_code == 400
        assert "crashed" in r.json()["detail"]
        session.expire_all()
        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        assert grant.status == "pending"                   # approval never landed
        row = (await session.execute(select(MarketplaceContainerModel))).scalars().one()
        assert row.status == "failed"
        assert "crashed" in row.error

    async def test_revoke_stops_container(self, client, session):
        grant = await self._pending(client, session)
        with _RuntimeMocks() as rt:
            await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": [], "scopes": []})
            r = await client.post(f"{MKT}/grants/{grant.id}/revoke", json={})
        assert r.status_code == 200
        assert rt.stopped == ["deadbeef" * 8]
        row = (await session.execute(select(MarketplaceContainerModel))).scalars().one()
        await session.refresh(row)
        assert row.status == "stopped"

    async def test_grant_detail_shows_container(self, client, session):
        grant = await self._pending(client, session)
        body = (await client.get(f"{MKT}/grants/{grant.id}")).json()
        assert body["container"]["image_digest"] == DIGEST
        assert body["container"]["status"] == "declared"


class TestSupplyChain:
    async def _pending(self, client, session):
        await _install_l2(client, session)
        return (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()

    async def test_signature_required_without_cosign_fails_closed(self, client, session):
        s = get_settings()
        old = s.marketplace_l2_require_signature
        s.marketplace_l2_require_signature = True
        try:
            grant = await self._pending(client, session)
            with patch("case_service.marketplace.runtime.shutil.which", return_value=None), \
                 patch("case_service.marketplace.runtime.pull_pinned_image"), \
                 patch("case_service.marketplace.runtime.provision_app_container"), \
                 patch("case_service.marketplace.runtime.container_running", return_value=False):
                r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                    "outbound_domains": [], "scopes": []})
            assert r.status_code == 400
            assert "cosign" in r.json()["detail"]
            session.expire_all()
            grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
            assert grant.status == "pending"               # approval refused
        finally:
            s.marketplace_l2_require_signature = old

    async def test_signature_verified_recorded(self, client, session):
        import subprocess as sp
        s = get_settings()
        old = s.marketplace_l2_require_signature
        s.marketplace_l2_require_signature = True
        try:
            grant = await self._pending(client, session)
            ok = sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("case_service.marketplace.runtime.shutil.which", return_value="/usr/bin/cosign"), \
                 patch("case_service.marketplace.runtime.subprocess.run", return_value=ok), \
                 patch("case_service.marketplace.runtime.pull_pinned_image"), \
                 patch("case_service.marketplace.runtime.provision_app_container",
                       return_value="deadbeef" * 8), \
                 patch("case_service.marketplace.runtime.container_running", return_value=False):
                r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                    "outbound_domains": [], "scopes": []})
            assert r.status_code == 200, r.text
            row = (await session.execute(select(MarketplaceContainerModel))).scalars().one()
            await session.refresh(row)
            assert row.signature_verified is True          # provenance
        finally:
            s.marketplace_l2_require_signature = old

    async def test_external_domains_refused_for_containers(self, client, session):
        """Egress isolation is by construction in this release — granting an
        external domain to a container would be unenforceable, so it refuses."""
        bundle = _l2_bundle(outbound_domains=["api.example.com"])
        _, r = await _install_l2(client, session, bundle)
        assert r.status_code == 200, r.text
        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        with patch("case_service.marketplace.runtime.pull_pinned_image"), \
             patch("case_service.marketplace.runtime.verify_image_signature", return_value=False), \
             patch("case_service.marketplace.runtime.container_running", return_value=False):
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": ["api.example.com"], "scopes": []})
        assert r.status_code == 400
        assert "egress-isolated" in r.json()["detail"]


class TestLayer2Drift:
    async def _granted(self, client, session):
        await _install_l2(client, session)
        grant = (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()
        with _RuntimeMocks():
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": [], "scopes": []})
            assert r.status_code == 200, r.text
        session.expire_all()
        return (await session.execute(select(MarketplaceCapabilityGrantModel))).scalars().one()

    async def test_new_digest_always_held_even_official(self, client, session):
        grant = await self._granted(client, session)
        out = await mkt_grants.apply_descriptor_drift(
            session, grant=grant,
            manifest=_l2_manifest(execution={
                "layer": 2, "image": "ghcr.io/acme/app:2.0", "image_digest": DIGEST2}),
            publisher_tier="official")                     # even official: new code
        assert out["action"] == "pending_reapproval"
        assert out["classification"] == "image_changed"
        assert grant.proposed["image_digest"] == DIGEST2

    async def test_drift_approve_swaps_digest_and_restarts(self, client, session):
        grant = await self._granted(client, session)
        await mkt_grants.apply_descriptor_drift(
            session, grant=grant,
            manifest=_l2_manifest(execution={
                "layer": 2, "image": "ghcr.io/acme/app:2.0", "image_digest": DIGEST2}),
            publisher_tier="community")
        await session.commit()
        grant_id = str(grant.id)
        with _RuntimeMocks() as rt:
            r = await client.post(f"{MKT}/grants/{grant_id}/drift/approve", json={
                "outbound_domains": [], "scopes": []})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "granted"
        assert body["descriptor_sha256"] == DIGEST2
        assert rt.stopped == ["deadbeef" * 8]              # old code stopped
        assert rt.provisioned[0]["digest"] == DIGEST2      # new code started
        row = (await session.execute(select(MarketplaceContainerModel))).scalars().one()
        await session.refresh(row)
        assert row.image_digest == DIGEST2
        assert row.image == "ghcr.io/acme/app:2.0"

    async def test_unchanged_digest_noop(self, client, session):
        grant = await self._granted(client, session)
        out = await mkt_grants.apply_descriptor_drift(
            session, grant=grant, manifest=_l2_manifest(), publisher_tier="community")
        assert out["action"] == "unchanged"
