#!/usr/bin/env python3
"""Velaris Marketplace Layer-2 — egress host-filter gateway.

A CONNECT-only forward proxy, and the ONLY way out of the internal
`velaris-apps` network. App containers hold a per-container credential
(minted at grant approval, hash-only at rest); every tunnel request is:

  1. authenticated  — Proxy-Authorization Basic, sha256(token) compared
                      constant-time against the allowlist entry
  2. host-filtered  — the target host must match the entry's admin-granted
                      domains (exact or subdomain, never substring)
  3. SSRF-guarded   — IP-literal hosts refused; every resolved address must
                      be globally routable (no private/loopback/link-local)
  4. audited        — one JSON line per attempt (allowed or blocked) to the
                      access log, ingested into marketplace_network_log

The allowlist (/etc/velaris-egress/allowlist.json) is re-read on every
request — writes by the platform take effect instantly, and a missing or
malformed file means DENY ALL. TLS stays end-to-end: this is a plain
byte tunnel, never a MITM.

Stdlib only. Runs as nobody in a read-only python:alpine container.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import time

CONF_PATH = os.environ.get("VELARIS_EGRESS_CONF", "/etc/velaris-egress/allowlist.json")
LOG_PATH = os.environ.get("VELARIS_EGRESS_LOG", "/var/log/velaris-egress/egress-access.log")
LISTEN_PORT = int(os.environ.get("VELARIS_EGRESS_PORT", "3128"))
ALLOWED_PORTS = {443, 80}
MAX_HEADER_BYTES = 8192
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 300
MAX_TUNNELS_PER_APP = 32          # a hostile container must not exhaust the gateway

_active_tunnels: dict[str, int] = {}


def load_allowlist() -> dict:
    """{'apps': {user: {'token_hash', 'domains', 'grant_id', 'package_id'}}}.
    Any problem reading it = empty = deny all (fail-closed)."""
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            data = json.load(f)
        apps = data.get("apps")
        return apps if isinstance(apps, dict) else {}
    except Exception:
        return {}


def host_allowed(host: str, domains: list) -> bool:
    """Exact host or subdomain of a granted domain — never substring tricks.
    (Same semantics as grants.host_allowed on the platform side.)"""
    host = (host or "").lower().rstrip(".")
    for d in domains or []:
        d = str(d).lower().rstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def parse_basic_auth(value: str) -> tuple[str, str] | None:
    try:
        scheme, _, b64 = value.strip().partition(" ")
        if scheme.lower() != "basic":
            return None
        user, _, token = base64.b64decode(b64.strip()).decode().partition(":")
        return (user, token) if user and token else None
    except Exception:
        return None


def credential_valid(entry: dict, token: str) -> bool:
    stored = str(entry.get("token_hash") or "")
    if not stored:
        return False
    return secrets.compare_digest(stored, hashlib.sha256(token.encode()).hexdigest())


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def all_ips_global(infos) -> bool:
    """Every resolved address must be globally routable — a granted domain
    that resolves into the private/loopback space is refused (DNS rebinding
    or a hostile publisher pointing its domain at the platform)."""
    ips = {info[4][0] for info in infos}
    if not ips:
        return False
    return all(ipaddress.ip_address(ip).is_global for ip in ips)


def vetted_addresses(infos) -> list[str]:
    """The addresses we will actually dial. The upstream connect MUST use one
    of these — never the hostname again — or a second DNS resolution could
    return a private address after the check (rebinding TOCTOU)."""
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


def log_event(**fields) -> None:
    """One JSON line per attempt. Open-append-close so the platform can
    rotate the file out from under us safely."""
    fields["ts"] = time.time()
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, separators=(",", ":")) + "\n")
    except Exception:
        pass  # auditing must never take the data path down


async def _respond(writer: asyncio.StreamWriter, status: str) -> None:
    try:
        writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
    except Exception:
        pass


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                counter: dict, key: str) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=IDLE_TIMEOUT)
            if not chunk:
                break
            counter[key] += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    user = target = "?"
    try:
        head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=CONNECT_TIMEOUT)
        if len(head) > MAX_HEADER_BYTES:
            await _respond(writer, "431 Request Header Fields Too Large")
            return
        lines = head.decode("latin-1").split("\r\n")
        method, _, rest = lines[0].partition(" ")
        target = rest.split(" ", 1)[0]
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if _:
                headers[k.strip().lower()] = v.strip()

        if method.upper() != "CONNECT":
            log_event(user=user, target=target, status="blocked", reason="non-connect method")
            await _respond(writer, "405 Method Not Allowed")
            return

        creds = parse_basic_auth(headers.get("proxy-authorization", ""))
        apps = load_allowlist()
        entry = apps.get(creds[0]) if creds else None
        if not creds or not entry or not credential_valid(entry, creds[1]):
            log_event(user=(creds[0] if creds else "?"), target=target,
                      status="blocked", reason="bad credentials")
            await _respond(writer, "407 Proxy Authentication Required")
            return
        user = creds[0]

        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            host, port = target, 443
        host = host.strip("[]").lower()

        def blocked(reason: str) -> None:
            log_event(user=user, grant_id=entry.get("grant_id"),
                      package_id=entry.get("package_id"), target=f"{host}:{port}",
                      status="blocked", reason=reason)

        if port not in ALLOWED_PORTS:
            blocked("port not allowed")
            await _respond(writer, "403 Forbidden")
            return
        if is_ip_literal(host):
            blocked("ip-literal target")
            await _respond(writer, "403 Forbidden")
            return
        if not host_allowed(host, entry.get("domains") or []):
            blocked("host not in granted domains")
            await _respond(writer, "403 Forbidden")
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port)
        except Exception:
            blocked("dns resolution failed")
            await _respond(writer, "502 Bad Gateway")
            return
        if not all_ips_global(infos):
            blocked("resolved to non-global address")
            await _respond(writer, "403 Forbidden")
            return
        if _active_tunnels.get(user, 0) >= MAX_TUNNELS_PER_APP:
            blocked("tunnel limit reached")
            await _respond(writer, "429 Too Many Requests")
            return

        # Dial the VETTED addresses only — never the hostname again (a second
        # resolution after the check would reopen the rebinding window).
        r_reader = r_writer = None
        for ip in vetted_addresses(infos):
            try:
                r_reader, r_writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), timeout=CONNECT_TIMEOUT)
                break
            except Exception:
                continue
        if r_writer is None:
            blocked("upstream connect failed")
            await _respond(writer, "502 Bad Gateway")
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        counter = {"sent": 0, "received": 0}
        _active_tunnels[user] = _active_tunnels.get(user, 0) + 1
        try:
            await asyncio.gather(
                _pump(reader, r_writer, counter, "sent"),
                _pump(r_reader, writer, counter, "received"))
        finally:
            _active_tunnels[user] = max(0, _active_tunnels.get(user, 1) - 1)
        log_event(user=user, grant_id=entry.get("grant_id"),
                  package_id=entry.get("package_id"), target=f"{host}:{port}",
                  status="allowed", bytes_sent=counter["sent"],
                  bytes_received=counter["received"])
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT)
    print(f"velaris-egress-gw listening on :{LISTEN_PORT} "
          f"(conf={CONF_PATH}, log={LOG_PATH})", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
