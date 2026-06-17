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
