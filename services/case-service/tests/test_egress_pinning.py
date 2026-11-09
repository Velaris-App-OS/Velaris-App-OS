"""DNS rebinding: a tenant's database and mail hosts are checked AT CONNECT
TIME and connected by the checked address (2026-11-04).

The write-time check alone let a name pass and later resolve inward. The
platform operator's sources and mailboxes (default tenant) stay on the LAN.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import socket

import pytest

from velaris_shared.hxbridge import security


def _resolves_to(monkeypatch, *addrs):
    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in addrs]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


async def test_a_public_name_is_pinned_to_its_address(monkeypatch):
    _resolves_to(monkeypatch, "8.8.8.8")
    assert await security.pinned_public_ip("db.example.com") == "8.8.8.8"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0"])
async def test_a_name_that_resolves_inward_is_refused(monkeypatch, ip):
    _resolves_to(monkeypatch, "8.8.8.8", ip)          # one inward answer is enough
    with pytest.raises(ValueError):
        await security.pinned_public_ip("rebind.example.com")


async def test_a_tenant_source_connects_by_the_pinned_address(monkeypatch):
    from case_service.hxdbmigrate import source
    seen = {}
    monkeypatch.setattr(source, "validate_source_host", lambda h: _noop())
    _resolves_to(monkeypatch, "8.8.4.4")
    monkeypatch.setattr(source, "_build_url", lambda st, host, *a: seen.setdefault("host", host) or "x")

    class _Stop(Exception):
        pass

    def _engine(*a, **k):
        raise _Stop()
    monkeypatch.setattr(source, "create_async_engine", _engine)
    with pytest.raises(_Stop):
        async with source.source_session("postgresql", "db.example.com", 5432, "d", "u", "p",
                                         public_only=True):
            pass
    assert seen["host"] == "8.8.4.4"
    _resolves_to(monkeypatch, "10.1.2.3")
    with pytest.raises(source.SourceError):
        async with source.source_session("postgresql", "db.example.com", 5432, "d", "u", "p",
                                         public_only=True):
            pass


async def _noop():
    return None


async def test_the_operators_mailbox_is_unchanged_and_a_tenants_is_pinned(monkeypatch, session):
    from case_service.mail import egress
    from velaris_shared.db.models import TenantModel
    import uuid
    session.add(TenantModel(id=uuid.uuid4(), slug="default", name="Default"))
    session.add(TenantModel(id=uuid.uuid4(), slug="acme", name="Acme"))
    await session.commit()
    from velaris_shared.tenancy import identity
    identity.clear_cache()
    _resolves_to(monkeypatch, "192.168.0.20")
    assert await egress.connect_host("mail.lan", None, verified_tls=False, session=session) == "mail.lan"
    assert await egress.connect_host("mail.lan", "default", verified_tls=False, session=session) == "mail.lan"
    with pytest.raises(ValueError):
        await egress.connect_host("mail.lan", "acme", verified_tls=False, session=session)
    _resolves_to(monkeypatch, "8.8.8.8")
    assert await egress.connect_host("mail.example.com", "acme", verified_tls=False,
                                     session=session) == "8.8.8.8"
    assert await egress.connect_host("mail.example.com", "acme", verified_tls=True,
                                     session=session) == "mail.example.com"
