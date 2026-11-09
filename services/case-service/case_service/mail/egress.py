"""Where a mailbox may connect (2026-11-04).

A tenant's SMTP/IMAP host is checked when it is saved (email router), but a
name can pass that check and later resolve inward (DNS rebinding). So at
connect time, a mailbox of any tenant but the default (the platform operator's,
which legitimately sits on the LAN) is checked again, and — where no verified
TLS would catch a host the name rebinds to — connected by the checked address.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations


async def _is_tenant_mailbox(tenant_id, session_factory=None, session=None) -> bool:
    from velaris_shared.tenancy.identity import DEFAULT_TENANT, same_tenant_as_default
    ref = tenant_id or DEFAULT_TENANT
    if session is not None:
        return not await same_tenant_as_default(session, ref)
    if session_factory is None:
        from velaris_shared.db.session import get_session_factory
        session_factory = get_session_factory()
    async with session_factory() as s:
        return not await same_tenant_as_default(s, ref)


async def connect_host(host: str, tenant_id, *, verified_tls: bool,
                       session=None, session_factory=None) -> str:
    """The host to connect to. Raises ValueError for a tenant mailbox whose
    host is not public now."""
    if not await _is_tenant_mailbox(tenant_id, session_factory, session):
        return host
    from velaris_shared.hxbridge.security import pinned_public_ip, validate_outbound_host
    if verified_tls:
        await validate_outbound_host(host)       # the certificate check names the host
        return host
    return await pinned_public_ip(host)
