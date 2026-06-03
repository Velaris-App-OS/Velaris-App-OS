"""Network egress enforcement for sandbox containers.

Applies iptables rules inside a sandbox container:
  - Default: DROP all outbound traffic
  - Whitelist: ACCEPT specific approved domains (resolved to IPs at approval time)
  - Logs every call attempt via a proxy sidecar (Phase 4B)

PERMANENTLY BLOCKED (cannot be whitelisted):
  10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16

Called by sandbox.py when:
  - A workspace container starts (apply DROP default)
  - An admin approves a whitelist entry (add ACCEPT rule)
  - A workspace is destroyed (rules auto-removed with container)
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
from typing import Sequence

logger = logging.getLogger(__name__)

# Internal ranges that can NEVER be whitelisted
BLOCKED_NETWORKS: list[ipaddress.IPv4Network] = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),   # cloud metadata
]


def is_blocked_ip(ip: str) -> bool:
    """Return True if the IP is in a permanently blocked range."""
    try:
        addr = ipaddress.IPv4Address(ip)
        return any(addr in net for net in BLOCKED_NETWORKS)
    except ValueError:
        return False


def resolve_domain(domain: str) -> list[str]:
    """Resolve a domain to its current IP addresses."""
    try:
        infos = socket.getaddrinfo(domain, None)
        return list({info[4][0] for info in infos})
    except socket.gaierror as e:
        logger.warning("Failed to resolve domain %s: %s", domain, e)
        return []


def apply_default_drop(container_id: str) -> None:
    """Apply iptables OUTPUT DROP default inside the container.

    This is called once when the sandbox container starts.
    All outbound traffic is blocked by default — whitelist entries add ACCEPT rules.
    """
    _run_in_container(container_id, ["iptables", "-P", "OUTPUT", "DROP"])
    # Also block FORWARD chain
    _run_in_container(container_id, ["iptables", "-P", "FORWARD", "DROP"])
    # Allow loopback (container-internal)
    _run_in_container(container_id, ["iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"])
    logger.info("Applied egress DROP default to container %s", container_id)


def add_whitelist_rule(container_id: str, domain: str) -> list[str]:
    """Add an ACCEPT rule for an admin-approved domain.

    Resolves the domain to IPs and adds per-IP ACCEPT rules.
    Raises ValueError if any resolved IP is in a blocked range.
    Returns list of IPs that were whitelisted.
    """
    ips = resolve_domain(domain)
    if not ips:
        raise ValueError(f"Could not resolve domain '{domain}' to any IP address")

    blocked = [ip for ip in ips if is_blocked_ip(ip)]
    if blocked:
        raise ValueError(
            f"Domain '{domain}' resolves to blocked internal IP(s): {blocked}. "
            "Internal IP ranges cannot be whitelisted."
        )

    allowed = []
    for ip in ips:
        _run_in_container(container_id, [
            "iptables", "-I", "OUTPUT", "1",
            "-d", ip, "-j", "ACCEPT",
        ])
        allowed.append(ip)
        logger.info("Whitelisted IP %s (%s) for container %s", ip, domain, container_id)

    return allowed


def remove_whitelist_rule(container_id: str, domain: str) -> None:
    """Remove ACCEPT rules for a domain (called when whitelist entry is denied/revoked)."""
    ips = resolve_domain(domain)
    for ip in ips:
        try:
            _run_in_container(container_id, [
                "iptables", "-D", "OUTPUT",
                "-d", ip, "-j", "ACCEPT",
            ])
        except Exception:
            pass   # Rule may not exist — ignore


def _run_in_container(container_id: str, cmd: Sequence[str]) -> str:
    """Execute a command inside a running container via docker exec."""
    full_cmd = ["docker", "exec", container_id] + list(cmd)
    result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"iptables command failed in container {container_id}: {result.stderr.strip()}"
        )
    return result.stdout.strip()
