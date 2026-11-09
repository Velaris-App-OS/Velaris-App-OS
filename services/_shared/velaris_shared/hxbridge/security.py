"""SSRF guard for outbound connector URLs.

Applied to user-configurable connectors (webhook, http) before any network
call. Typed built-in connectors (docling, twilio, s3) use hardcoded or
admin-controlled URLs and are exempt.

Blocks:
  - Non-http/https schemes
  - Known private hostnames (localhost, metadata endpoints)
  - RFC-1918, loopback, link-local, carrier-grade NAT, and IPv6 private ranges
  - Hostnames that fail DNS resolution

DNS resolution runs in a thread-pool executor to avoid blocking the event loop.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),       # RFC-1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC-1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC-1918 private
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("0.0.0.0/8"),         # "this host" — 0.0.0.0 reaches loopback services
    ipaddress.ip_network("169.254.0.0/16"),    # link-local / AWS metadata
    ipaddress.ip_network("100.64.0.0/10"),     # carrier-grade NAT (RFC 6598)
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA private
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
)

_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "0.0.0.0",
    "metadata.google.internal",
    "169.254.169.254",
})


def _resolve_and_check(hostname: str) -> None:
    """DNS-resolve hostname and raise ValueError if any result is a private IP.

    Runs synchronously — call via run_in_executor to avoid blocking the loop.

    IPv4-mapped IPv6 addresses (::ffff:x.x.x.x) are also checked against IPv4
    networks — simply checking addr in net returns False for this combination
    even when the embedded IPv4 address is private.
    """
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(
            f"Connector URL hostname cannot be resolved: {hostname!r}"
        ) from exc

    for _family, _type, _proto, _canon, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        # Build the set of addresses to check: the address itself plus the
        # mapped IPv4 address if this is an IPv4-mapped IPv6 (::ffff:x.x.x.x).
        # The ipaddress module returns False (not an error) when checking an
        # IPv6Address against an IPv4Network, so we must unwrap the mapping.
        candidates = [addr]
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            candidates.append(addr.ipv4_mapped)

        for candidate in candidates:
            for net in _BLOCKED_NETWORKS:
                try:
                    if candidate in net:
                        raise ValueError(
                            f"Connector URL resolves to a private/internal address ({ip_str})"
                        )
                except TypeError:
                    pass  # version mismatch (IPv4 addr vs IPv6 net) — not the same family


async def validate_outbound_url(url: str) -> None:
    """Raise ValueError if url targets private or internal infrastructure.

    Checks scheme, hostname blocklist, then DNS-resolved IP ranges.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Connector URL must use http or https, got {parsed.scheme!r}"
        )

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("Connector URL must include a hostname")

    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Connector URL targets a blocked host: {hostname!r}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _resolve_and_check, hostname)


async def validate_outbound_host(host: str) -> None:
    """Raise ValueError if a bare host (a database, SMTP or IMAP server a
    tenant configures) is private or internal infrastructure (2026-11-04).

    Same rules as `validate_outbound_url`. Without it a tenant admin — a
    website demo account among them — pointed HxDBMigrate at the platform's own
    PostgreSQL on 127.0.0.1 and read "password authentication failed": a
    password oracle and a LAN port scanner. The platform operator, who
    legitimately runs its databases and mail servers on the LAN, is not
    checked by the callers.
    """
    hostname = (host or "").strip().strip("[]").lower()
    if not hostname:
        raise ValueError("A host is required")
    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(f"That host is not allowed: {hostname!r}")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _resolve_and_check, hostname)


def _pinned_sync(hostname: str) -> str:
    _resolve_and_check(hostname)                 # every address public, or ValueError
    for *_x, sockaddr in socket.getaddrinfo(hostname, None):
        return sockaddr[0]
    raise ValueError(f"Host cannot be resolved: {hostname!r}")


async def pinned_public_ip(host: str) -> str:
    """Resolve `host` once, refuse unless every address is public, and return
    the address to connect to (2026-11-04). Connecting to THIS address, not the
    name, closes DNS rebinding — a name that passes the check and then
    resolves inward for the connection."""
    hostname = (host or "").strip().strip("[]").lower()
    if not hostname or hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(f"That host is not allowed: {hostname!r}")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _pinned_sync, hostname)


#: Config keys that name where a connector sends its requests (2026-11-04).
CONNECTOR_URL_KEYS = ("url", "base_url", "endpoint_url", "instance_url", "webhook_url", "api_base")


async def platform_owned(session, connector_tenant) -> bool:
    """The operator's connector (default tenant, or none): may reach the LAN."""
    from velaris_shared.tenancy.identity import DEFAULT_TENANT, same_tenant_as_default
    if connector_tenant is None or str(connector_tenant).strip() in ("", DEFAULT_TENANT):
        return True
    return await same_tenant_as_default(session, str(connector_tenant))


def runtime_config(config: dict | None, owned_by_platform: bool) -> dict:
    """The config a connector instance is built with: the saved one plus
    `_platform_owned`, which only this code sets (a saved value is dropped)."""
    out = {k: v for k, v in (config or {}).items() if k != "_platform_owned"}
    out["_platform_owned"] = bool(owned_by_platform)
    return out


async def check_connector_targets(session, connector_tenant, config: dict | None) -> None:
    """Raise ValueError if a TENANT's connector names an internal address.

    The operator's (default tenant / no tenant) keep their LAN targets — an
    internal Docling or MinIO is normal there. Anyone else's connector — a
    website demo tenant's above all — may only reach the internet: before this,
    one pointed at 127.0.0.1 read Studio's own page back (2026-11-04). A value
    with {placeholders} is checked again by the connector after they are filled.
    """
    if await platform_owned(session, connector_tenant):
        return
    for key in CONNECTOR_URL_KEYS:
        val = (config or {}).get(key)
        if isinstance(val, str) and val.strip() and "{" not in val:
            await validate_outbound_url(val.strip())


async def check_connector_config(session, connector_type: str, connector_tenant, config: dict | None) -> None:
    """Everything a saved connector config must pass, wherever it is written
    (the API, a branch merge, an AI draft, a package import): well-formed
    operations, no reserved key, and — for a tenant's connector — only public
    targets. Raises ValueError (OperationError is one) naming the problem."""
    from velaris_shared.hxbridge.operations import validate_operations
    validate_operations(connector_type, config)
    await check_connector_targets(session, connector_tenant, config)
    if connector_type == "sql_query":
        await check_sql_target(session, connector_tenant, config)


async def check_sql_target(session, connector_tenant, config: dict | None) -> None:
    """A database query connector's settings, and — for a tenant's — a public
    database host (2026-11-08). The operator's may name its LAN, as an
    HxDBMigrate source may. Checked again, and pinned, at every connect."""
    from velaris_shared.hxbridge.sql_query import check_settings
    settings = check_settings(config)
    if not await platform_owned(session, connector_tenant):
        try:
            await validate_outbound_host(settings["host"])
        except ValueError:
            raise ValueError("That database host is not reachable from this platform: "
                             "private and internal addresses are refused.") from None


async def usable_by(session, connector, tenant) -> bool:
    """May `tenant` use this connector? Its own, or — for the default tenant
    alone — the operator's (no tenant): an operator connector carries the
    operator's credentials and accounts (2026-11-04)."""
    from velaris_shared.tenancy.identity import same_tenant
    if connector.tenant_id is None:
        return await platform_owned(session, tenant)
    return tenant is not None and await same_tenant(session, connector.tenant_id, str(tenant))


async def pinned_request_args(url: str) -> tuple[str, dict, dict]:
    """Check `url` and pin it (2026-11-04): returns (url with the host replaced
    by the ONE public address it was checked at, the Host header, the TLS name).
    Sending to that address closes DNS rebinding — a name that passes the check
    and then resolves inward for the connection. The certificate is still
    checked against the name (httpx `sni_hostname`)."""
    from urllib.parse import urlsplit, urlunsplit
    await validate_outbound_url(url)
    parts = urlsplit(url)
    host = parts.hostname or ""
    ip = await pinned_public_ip(host)
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc_ip += f":{parts.port}"
    host_header = host + (f":{parts.port}" if parts.port else "")
    pinned = urlunsplit((parts.scheme, netloc_ip, parts.path, parts.query, parts.fragment))
    return pinned, {"Host": host_header}, {"sni_hostname": host}
