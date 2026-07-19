"""Marketplace Layer-2 — egress host-filter (granted outbound domains).

Pins: fail-closed gating (flag OFF or gateway down = domain grants refused
exactly as pre-egress, grant stays pending), credential mint (hash-only at
rest, raw only in the injected proxy env), allowlist lifecycle (upsert on
approval, removal on revoke AND on provision failure AND on narrowing),
proxy decision logic (auth, host allowlist semantics, IP-literal + private
resolution refusal, deny-all on missing config), and access-log ingest into
marketplace_network_log (grant-anchored, blocked = undeclared, rotate-safe).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from case_service.config import get_settings
from case_service.db.models import (
    MarketplaceCapabilityGrantModel,
    MarketplaceNetworkLogModel,
)
from case_service.marketplace import egress, runtime

from .test_capability_grants import MKT
from .test_layer2 import DIGEST, _l2_bundle, _install_l2, _RuntimeMocks

_PROXY_PATH = (Path(__file__).resolve().parents[4]
               / "deploy" / "docker-compose" / "egress-gw" / "proxy.py")


def _load_proxy():
    spec = importlib.util.spec_from_file_location("velaris_egress_proxy", _PROXY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def egress_dir(tmp_path):
    """Point the platform's egress dir at a tmp sandbox and enable the flag."""
    s = get_settings()
    old_dir, old_flag = s.marketplace_l2_egress_dir, s.marketplace_l2_egress_enabled
    s.marketplace_l2_egress_dir = str(tmp_path)
    s.marketplace_l2_egress_enabled = True
    try:
        yield tmp_path
    finally:
        s.marketplace_l2_egress_dir = old_dir
        s.marketplace_l2_egress_enabled = old_flag


def _allowlist(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "conf" / "allowlist.json").read_text())["apps"]


class TestEgressModule:
    def test_mint_hash_only(self):
        raw, token_hash = egress.mint_credentials()
        assert token_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in token_hash

    def test_upsert_and_remove(self, egress_dir):
        egress.upsert_app("app-1", token_hash="h", grant_id="g", package_id="p",
                          tenant_id="t", domains=["API.Example.com"])
        apps = _allowlist(egress_dir)
        assert apps["app-1"]["domains"] == ["api.example.com"]     # normalized
        assert apps["app-1"]["token_hash"] == "h"
        egress.remove_app("app-1")
        assert _allowlist(egress_dir) == {}
        egress.remove_app("app-1")                                 # idempotent
        egress.remove_app(None)

    def test_proxy_env_shape(self):
        env = egress.proxy_env("app-1", "RAWTOKEN")
        assert env["HTTPS_PROXY"] == "http://app-1:RAWTOKEN@velaris-egress-gw:3128"
        assert env["HTTP_PROXY"] == env["HTTPS_PROXY"]
        assert "velaris-broker-gw" in env["NO_PROXY"]              # broker never proxied


class TestProxyDecisions:
    def test_host_allowed_never_substring(self):
        proxy = _load_proxy()
        assert proxy.host_allowed("api.example.com", ["api.example.com"])
        assert proxy.host_allowed("sub.api.example.com", ["api.example.com"])
        assert not proxy.host_allowed("evilapi.example.com", ["api.example.com"])
        assert not proxy.host_allowed("api.example.com.evil.net", ["api.example.com"])
        assert not proxy.host_allowed("api.example.com", [])

    def test_auth_and_credentials(self):
        proxy = _load_proxy()
        b64 = base64.b64encode(b"user:tok").decode()
        assert proxy.parse_basic_auth(f"Basic {b64}") == ("user", "tok")
        assert proxy.parse_basic_auth("Bearer xyz") is None
        assert proxy.parse_basic_auth("") is None
        entry = {"token_hash": hashlib.sha256(b"tok").hexdigest()}
        assert proxy.credential_valid(entry, "tok")
        assert not proxy.credential_valid(entry, "wrong")
        assert not proxy.credential_valid({}, "tok")               # no hash = never

    def test_ip_literals_and_private_resolution(self):
        proxy = _load_proxy()
        assert proxy.is_ip_literal("10.0.0.1")
        assert proxy.is_ip_literal("[::1]")
        assert not proxy.is_ip_literal("api.example.com")
        pub = [(0, 0, 0, "", ("93.184.216.34", 443))]
        priv = [(0, 0, 0, "", ("172.31.101.1", 443))]
        assert proxy.all_ips_global(pub)
        assert not proxy.all_ips_global(priv)
        assert not proxy.all_ips_global(pub + priv)                # ANY private = deny
        assert not proxy.all_ips_global([])

    def test_upstream_dial_uses_vetted_ips_only(self):
        """Rebinding TOCTOU pin: the connect loop dials vetted_addresses(),
        never the hostname — the vetted list must be exactly the checked IPs."""
        proxy = _load_proxy()
        infos = [(0, 0, 0, "", ("93.184.216.34", 443)),
                 (0, 0, 0, "", ("93.184.216.34", 443)),            # dedup
                 (0, 0, 0, "", ("2606:2800:220:1::1", 443))]
        assert proxy.vetted_addresses(infos) == ["93.184.216.34", "2606:2800:220:1::1"]
        src = _PROXY_PATH.read_text()
        assert "open_connection(ip, port)" in src                  # dials the IP
        assert "open_connection(host, port)" not in src            # never the name

    def test_missing_config_denies_all(self, monkeypatch, tmp_path):
        proxy = _load_proxy()
        monkeypatch.setattr(proxy, "CONF_PATH", str(tmp_path / "nope.json"))
        assert proxy.load_allowlist() == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        monkeypatch.setattr(proxy, "CONF_PATH", str(bad))
        assert proxy.load_allowlist() == {}


DOMAINS = ["api.example.com"]


class TestDomainGrantGating:
    async def _pending(self, client, session):
        await _install_l2(client, session, _l2_bundle(outbound_domains=DOMAINS))
        return (await session.execute(
            select(MarketplaceCapabilityGrantModel))).scalars().one()

    async def test_flag_off_refuses_domain_grant(self, client, session):
        """Pre-egress behavior preserved: real provision path, no docker touched
        (the refusal fires before any client call)."""
        grant = await self._pending(client, session)
        assert not egress.egress_enabled()                         # default OFF
        with patch("case_service.marketplace.runtime.verify_image_signature",
                   return_value=False), \
             patch("case_service.marketplace.runtime.pull_pinned_image"), \
             patch("case_service.marketplace.runtime.container_running",
                   return_value=False):
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": DOMAINS, "scopes": []})
        assert r.status_code == 400
        assert "egress is disabled" in r.json()["detail"]
        session.expire_all()
        grant = (await session.execute(
            select(MarketplaceCapabilityGrantModel))).scalars().one()
        assert grant.status == "pending"                           # never landed

    async def test_gateway_down_refuses_domain_grant(self, client, session, egress_dir):
        grant = await self._pending(client, session)
        with patch("case_service.marketplace.runtime.verify_image_signature",
                   return_value=False), \
             patch("case_service.marketplace.runtime.pull_pinned_image"), \
             patch("case_service.marketplace.runtime.container_running",
                   return_value=False), \
             patch("case_service.marketplace.egress.require_gateway_running",
                   side_effect=runtime.Layer2Error(
                       "egress gateway 'velaris-egress-gw' is not available")):
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": DOMAINS, "scopes": []})
        assert r.status_code == 400
        assert "egress gateway" in r.json()["detail"]
        assert _allowlist(egress_dir) == {}                        # entry cleaned up

    async def test_approve_mints_credential_and_allowlists(self, client, session, egress_dir):
        grant = await self._pending(client, session)
        with _RuntimeMocks() as rt:
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": DOMAINS, "scopes": []})
        assert r.status_code == 200, r.text
        session.expire_all()
        grant = (await session.execute(
            select(MarketplaceCapabilityGrantModel))).scalars().one()
        user = grant.granted["egress_user"]
        token_hash = grant.granted["egress_token_hash"]
        apps = _allowlist(egress_dir)
        assert apps[user]["domains"] == DOMAINS
        assert apps[user]["token_hash"] == token_hash
        assert apps[user]["grant_id"] == str(grant.id)
        env = rt.provisioned[0]["egress_env"]
        assert env["HTTPS_PROXY"].startswith(f"http://{user}:")
        raw = env["HTTPS_PROXY"].split(":", 2)[2].split("@")[0]
        assert hashlib.sha256(raw.encode()).hexdigest() == token_hash
        assert raw not in json.dumps(grant.granted)                # hash-only at rest

    async def test_empty_domain_grant_gets_no_credential(self, client, session, egress_dir):
        await _install_l2(client, session)                         # no domains requested
        grant = (await session.execute(
            select(MarketplaceCapabilityGrantModel))).scalars().one()
        with _RuntimeMocks() as rt:
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": [], "scopes": []})
        assert r.status_code == 200
        assert rt.provisioned[0]["egress_env"] is None
        conf = egress_dir / "conf" / "allowlist.json"
        assert not conf.exists() or _allowlist(egress_dir) == {}
        session.expire_all()
        grant = (await session.execute(
            select(MarketplaceCapabilityGrantModel))).scalars().one()
        assert "egress_token_hash" not in (grant.granted or {})

    async def test_revoke_removes_allowlist_entry(self, client, session, egress_dir):
        grant = await self._pending(client, session)
        with _RuntimeMocks():
            await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": DOMAINS, "scopes": []})
            assert len(_allowlist(egress_dir)) == 1
            r = await client.post(f"{MKT}/grants/{grant.id}/revoke", json={})
        assert r.status_code == 200
        assert _allowlist(egress_dir) == {}                        # instant egress cut

    async def test_provision_failure_removes_entry(self, client, session, egress_dir):
        grant = await self._pending(client, session)
        with _RuntimeMocks(provision_fail=True):
            r = await client.post(f"{MKT}/grants/{grant.id}/approve", json={
                "outbound_domains": DOMAINS, "scopes": []})
        assert r.status_code == 400
        assert _allowlist(egress_dir) == {}                        # no orphaned egress


class TestAccessLogIngest:
    async def test_ingest_grant_anchored_and_rotates(self, client, session, egress_dir):
        grant = await TestDomainGrantGating()._pending(client, session)
        log = egress_dir / "log" / "egress-access.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"user": "app-x", "grant_id": str(grant.id),
                        "package_id": grant.package_id, "target": "api.example.com:443",
                        "status": "allowed", "bytes_sent": 10, "bytes_received": 20}),
            json.dumps({"user": "app-x", "grant_id": str(grant.id),
                        "package_id": grant.package_id, "target": "evil.example.net:443",
                        "status": "blocked", "reason": "host not in granted domains"}),
            "{corrupt line",
        ]
        log.write_text("\n".join(lines) + "\n")
        n = await egress.ingest_access_log(session)
        await session.commit()
        assert n == 2                                              # corrupt line skipped
        rows = (await session.execute(
            select(MarketplaceNetworkLogModel).order_by(
                MarketplaceNetworkLogModel.destination_url))).scalars().all()
        assert len(rows) == 2
        allowed = next(r for r in rows if r.status == "allowed")
        blocked = next(r for r in rows if r.status == "blocked")
        assert allowed.destination_url == "egress://api.example.com:443"
        assert allowed.grant_id == grant.id
        assert allowed.bytes_received == 20
        assert blocked.is_declared is False                        # SECURITY VIOLATION flag
        assert not log.exists()                                    # rotated away
        assert await egress.ingest_access_log(session) == 0        # nothing left

    async def test_missing_log_is_fine(self, session, egress_dir):
        assert await egress.ingest_access_log(session) == 0
