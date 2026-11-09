"""Two gaps found by the database-query security pass (2026-11-08).

1. HxDBMigrate `ssl_mode=verify` kept the host NAME for a tenant's source, so the
   driver resolved it again at connect — a rebinding name could still reach
   inward. Now the socket goes to the checked address and the certificate is
   checked against the name.
2. An oversized connector response was stored on disk with a ref URL
   (`/api/v1/hxbridge/overflow/<call id>`) that had no route. It is now read like
   the call history: connector read permission, own tenant's calls only.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import os
import socket
import uuid

import pytest_asyncio

from tests.conftest import TestSessionFactory, _headers
from velaris_shared.db.models import (
    ConnectorRegistryModel, IntegrationCallModel, TenantModel, UserDirectoryModel,
)
from velaris_shared.hxbridge.sql_query import NamedTLSContext


# ── 1. HxDBMigrate verified TLS is pinned ─────────────────────────────────────

async def _captured_session(monkeypatch, *, public_only: bool, ssl_mode: str, engine="postgresql"):
    from case_service.hxdbmigrate import source
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    seen = {}

    def fake_engine(url, **kw):
        seen.update(url=url, connect_args=kw["connect_args"])
        raise RuntimeError("stop before connecting")
    monkeypatch.setattr(source, "create_async_engine", fake_engine)
    try:
        async with source.source_session(engine, "db.example.com", None, "crm", "ro", "pw",
                                         ssl_mode, public_only=public_only):
            pass
    except RuntimeError:
        pass
    return seen


async def test_a_tenants_verified_source_connects_to_the_checked_address(monkeypatch):
    for engine in ("postgresql", "mysql"):
        seen = await _captured_session(monkeypatch, public_only=True, ssl_mode="verify", engine=engine)
        assert "@93.184.216.34:" in seen["url"], seen["url"]
        ctx = seen["connect_args"]["ssl"]
        assert isinstance(ctx, NamedTLSContext) and ctx.velaris_name == "db.example.com"
        assert ctx.check_hostname


async def test_other_modes_and_the_operator_are_as_before(monkeypatch):
    seen = await _captured_session(monkeypatch, public_only=True, ssl_mode="require")
    assert "@93.184.216.34:" in seen["url"] and seen["connect_args"]["ssl"] == "require"
    seen = await _captured_session(monkeypatch, public_only=False, ssl_mode="verify")
    assert "@db.example.com:" in seen["url"] and seen["connect_args"]["ssl"] == "verify-full"


# ── 2. the overflow route ─────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def overflow_call(tmp_path, monkeypatch):
    from velaris_shared.config import get_settings
    monkeypatch.setattr(get_settings(), "storage_local_path", str(tmp_path))
    suffix = uuid.uuid4().hex[:6]
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug=f"ovf-{suffix}", name="Overflow tenant")
        other = TenantModel(id=uuid.uuid4(), slug=f"ovo-{suffix}", name="Other tenant")
        db.add_all([t, other,
                    UserDirectoryModel(id=uuid.uuid4(), user_id=f"ovfa-{suffix}", tenant_id=t.slug),
                    UserDirectoryModel(id=uuid.uuid4(), user_id=f"ovfw-{suffix}", tenant_id=t.slug),
                    UserDirectoryModel(id=uuid.uuid4(), user_id=f"ovoa-{suffix}", tenant_id=other.slug)])
        c = ConnectorRegistryModel(name=f"big-{suffix}", connector_type="http_custom", config={},
                                   credentials={}, tenant_id=t.slug, enabled=True)
        db.add(c)
        await db.flush()
        call = IntegrationCallModel(connector_id=c.id, status="success", request={},
                                    response={"_overflow": True, "size_bytes": 9,
                                              "ref_url": "/api/v1/hxbridge/overflow/x"})
        plain = IntegrationCallModel(connector_id=c.id, status="success", request={}, response={"ok": 1})
        db.add_all([call, plain])
        await db.commit()
        os.makedirs(tmp_path / "connector-overflow")
        (tmp_path / "connector-overflow" / f"{call.id}.json").write_text(json.dumps({"rows": [1, 2, 3]}))
    return {"call": call.id, "plain": plain.id, "admin": _headers(f"ovfa-{suffix}", ["admin"]),
            "worker": _headers(f"ovfw-{suffix}", ["case_worker"]),
            "other": _headers(f"ovoa-{suffix}", ["admin"])}


async def test_the_tenants_admin_reads_its_large_response(client, overflow_call):
    r = await client.get(f"/api/v1/hxbridge/overflow/{overflow_call['call']}", headers=overflow_call["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["response"] == {"rows": [1, 2, 3]}


async def test_others_are_refused(client, anon_client, overflow_call):
    url = f"/api/v1/hxbridge/overflow/{overflow_call['call']}"
    r = await client.get(url, headers=overflow_call["other"])
    assert r.status_code == 404, r.text
    r = await client.get(url, headers=overflow_call["worker"])
    assert r.status_code == 403, r.text
    r = await anon_client.get(url)
    assert r.status_code == 401, r.text
    r = await client.get(f"/api/v1/hxbridge/overflow/{overflow_call['plain']}", headers=overflow_call["admin"])
    assert r.status_code == 404
    r = await client.get(f"/api/v1/hxbridge/overflow/{uuid.uuid4()}", headers=overflow_call["admin"])
    assert r.status_code == 404
