"""
HTTP Task Resolver
===================

Makes BPMN ServiceTasks call real HTTP APIs.

This is the first plugin resolver — it handles ServiceTasks whose
``implementation`` field contains an HTTP URL or a helix:// URI
that maps to a configured endpoint.

Implementation URI patterns::

    # Direct HTTP call
    implementation="https://api.example.com/orders/validate"
    implementation="http://localhost:3000/webhook"

    # Helix service URI (resolved via service registry)
    implementation="helix://order-service/validate"
    implementation="helix://notification-service/send"

The resolver:
  1. Checks if it can handle the task (HTTP URL or helix:// URI).
  2. Builds the HTTP request from task extensions and process variables.
  3. Calls the endpoint with httpx.
  4. Returns the response as updated process variables.

Configuration via task extensions::

    <serviceTask id="call_api" name="Call API"
                 implementation="https://api.example.com/orders">
      <extensionElements>
        <helix:properties>
          <helix:property name="helix:method" value="POST"/>
          <helix:property name="helix:headers" value='{"Authorization": "Bearer ${api_token}"}'/>
          <helix:property name="helix:body" value='{"order_id": "${order_id}"}'/>
          <helix:property name="helix:timeout" value="30"/>
          <helix:property name="helix:resultVariable" value="api_response"/>
        </helix:properties>
      </extensionElements>
    </serviceTask>

Variable substitution:
    Any ``${variable_name}`` in the URL, headers, or body is replaced
    with the corresponding process variable value.

Usage::

    from helix_engine.plugins.http_resolver import HttpTaskResolver

    resolver = HttpTaskResolver()
    # Or with a service registry:
    resolver = HttpTaskResolver(service_registry={
        "order-service": "http://localhost:3001",
        "notification-service": "http://localhost:3002",
    })
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from helix_ir.models.process import ServiceTask, SendTask, _TaskBase

logger = structlog.get_logger()

# Variable substitution pattern: ${variable_name}
_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')


# ═══════════════════════════════════════════════════════════════════════
#  Sensitive variable filter
# ═══════════════════════════════════════════════════════════════════════

# Key substrings that identify variables that must never be sent to any
# external endpoint — regardless of helix:body or URL substitution.
# Matched case-insensitively against the full variable name.
_SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "_key",
    "_token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "auth_token",
    "access_token",
    "refresh_token",
    "jwt",
    "bearer",
    "signing_key",
    "encryption_key",
)


def _redact_sensitive(variables: dict[str, Any]) -> dict[str, Any]:
    """
    Return a copy of variables with any key that looks like a secret removed.

    This is a defence-in-depth layer: it prevents a secret that somehow
    ended up in process variables from being exfiltrated via URL substitution,
    helix:body, helix:headers, or helix:params on external service tasks.

    Called only for external (non-helix://) HTTP requests.
    """
    clean: dict[str, Any] = {}
    redacted: list[str] = []
    for k, v in variables.items():
        lower = k.lower()
        if any(frag in lower for frag in _SECRET_KEY_FRAGMENTS):
            redacted.append(k)
        else:
            clean[k] = v
    if redacted:
        logger.warning(
            "service_task_redacted_sensitive_vars",
            count=len(redacted),
            keys=redacted,
        )
    return clean


# ═══════════════════════════════════════════════════════════════════════
#  SSRF protection
# ═══════════════════════════════════════════════════════════════════════

class _SSRFBlockedError(ValueError):
    """Raised when a service task URL targets a private or internal address."""


# Every IP range that must never be reachable from a workflow service task.
# This covers: loopback, RFC-1918 private, link-local (cloud metadata),
# carrier-grade NAT, IPv6 equivalents, and all IANA reserved ranges.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),        # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),          # RFC 1918 private
    ipaddress.ip_network("172.16.0.0/12"),       # RFC 1918 private
    ipaddress.ip_network("192.168.0.0/16"),      # RFC 1918 private
    ipaddress.ip_network("169.254.0.0/16"),      # Link-local / cloud metadata
                                                  # (AWS: 169.254.169.254,
                                                  #  GCP: 169.254.169.254,
                                                  #  Azure: 169.254.169.254)
    ipaddress.ip_network("100.64.0.0/10"),       # Carrier-grade NAT (RFC 6598)
    ipaddress.ip_network("0.0.0.0/8"),           # Current network (RFC 1122)
    ipaddress.ip_network("192.0.0.0/24"),        # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),        # TEST-NET-1 (RFC 5737)
    ipaddress.ip_network("198.18.0.0/15"),       # Benchmarking (RFC 2544)
    ipaddress.ip_network("198.51.100.0/24"),     # TEST-NET-2 (RFC 5737)
    ipaddress.ip_network("203.0.113.0/24"),      # TEST-NET-3 (RFC 5737)
    ipaddress.ip_network("240.0.0.0/4"),         # Reserved (RFC 1112)
    ipaddress.ip_network("255.255.255.255/32"),  # Broadcast
    # IPv6
    ipaddress.ip_network("::1/128"),             # IPv6 loopback
    ipaddress.ip_network("::ffff:0:0/96"),       # IPv4-mapped IPv6
    ipaddress.ip_network("64:ff9b::/96"),        # IPv4/IPv6 translation
    ipaddress.ip_network("fe80::/10"),            # IPv6 link-local
    ipaddress.ip_network("fc00::/7"),             # IPv6 unique-local (RFC 4193)
    ipaddress.ip_network("::/128"),              # Unspecified
    ipaddress.ip_network("2001:db8::/32"),       # Documentation (RFC 3849)
)

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({
    "localhost",
    "metadata.google.internal",  # GCP metadata
    "metadata.internal",
    "169.254.169.254",           # Cloud metadata IP (string form)
})

_BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (
    ".local",       # mDNS / LAN hosts
    ".internal",    # Private DNS zones
    ".localhost",   # RFC 2606
    ".test",        # RFC 2606
    ".example",     # RFC 2606
    ".invalid",     # RFC 2606
    ".lan",
    ".corp",
    ".home",
    ".intranet",
)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP falls in any blocked network range."""
    for network in _BLOCKED_NETWORKS:
        try:
            if ip in network:
                return True
        except TypeError:
            pass  # IPv4 vs IPv6 mismatch — definitely not in this network
    return False


async def _assert_url_safe(url: str) -> None:
    """
    Raise _SSRFBlockedError if the URL resolves to a private, loopback,
    link-local, or cloud-metadata address.

    Only called for direct http:// and https:// implementations.
    helix:// URIs resolve through the operator-configured service registry
    and are trusted without this check.

    Note: DNS rebinding is a known limitation of pre-connect validation.
    For environments requiring full protection, use a network-level egress
    policy (e.g. iptables, security groups) in addition to this check.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise _SSRFBlockedError(f"Malformed URL: {exc}") from exc

    if parsed.scheme not in ("http", "https"):
        raise _SSRFBlockedError(
            f"Only http:// and https:// are permitted for external calls; "
            f"got '{parsed.scheme}://'"
        )

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        raise _SSRFBlockedError("URL contains no resolvable hostname")

    # Fast-path: reject by known hostname
    if hostname in _BLOCKED_HOSTNAMES:
        raise _SSRFBlockedError(
            f"Access to '{hostname}' is blocked (internal/metadata host)"
        )

    # Reject by domain suffix
    for suffix in _BLOCKED_HOST_SUFFIXES:
        if hostname.endswith(suffix):
            raise _SSRFBlockedError(
                f"Access to '{hostname}' is blocked (suffix '{suffix}' is not permitted)"
            )

    # If it's already a raw IP literal, check it directly without DNS
    try:
        ip = ipaddress.ip_address(hostname)
        if _ip_is_blocked(ip):
            raise _SSRFBlockedError(
                f"Access to {ip} is blocked (private/reserved address space)"
            )
        return  # Valid public IP — done
    except ValueError:
        pass  # Not a raw IP — fall through to DNS resolution

    # DNS resolution — run in thread pool to avoid blocking the event loop
    try:
        loop = asyncio.get_event_loop()
        results: list = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM),
        )
    except socket.gaierror as exc:
        raise _SSRFBlockedError(
            f"Cannot resolve hostname '{hostname}': {exc}"
        ) from exc

    if not results:
        raise _SSRFBlockedError(f"No addresses returned for '{hostname}'")

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
            if _ip_is_blocked(ip):
                raise _SSRFBlockedError(
                    f"Access to '{hostname}' ({ip}) is blocked: "
                    f"resolves to a private/reserved address"
                )
        except ValueError:
            pass  # Shouldn't happen from getaddrinfo, but skip gracefully


class HttpTaskResolver:
    """
    Resolves BPMN ServiceTasks and SendTasks to HTTP API calls.

    Implements the ``TaskResolver`` protocol from
    ``helix_engine.runtime.task``.

    Args:
        service_registry: Maps helix service names to base URLs.
            Example: {"order-service": "http://localhost:3001"}
        default_timeout: Default HTTP timeout in seconds.
        default_headers: Headers added to every request.
    """

    def __init__(
        self,
        service_registry: dict[str, str] | None = None,
        default_timeout: float = 30.0,
        default_headers: dict[str, str] | None = None,
    ):
        self._registry = service_registry or {}
        self._default_timeout = default_timeout
        self._default_headers = default_headers or {}
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init the httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._default_timeout),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── TaskResolver protocol ─────────────────────────────────────

    async def can_handle(self, task: _TaskBase) -> bool:
        """
        Returns True for ServiceTasks and SendTasks with HTTP or helix:// URIs.
        """
        if not isinstance(task, (ServiceTask, SendTask)):
            return False

        impl = getattr(task, "implementation", None)
        if not impl:
            return False

        return (
            impl.startswith("http://")
            or impl.startswith("https://")
            or impl.startswith("helix://")
        )

    async def resolve(self, task: _TaskBase, variables: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the HTTP call and return updated variables.

        The response is stored in the variable named by ``helix:resultVariable``
        (default: ``_result_{task_id}``).
        """
        impl = getattr(task, "implementation", "")
        extensions = task.extensions
        is_helix_uri = impl.startswith("helix://")

        # For external (non-helix://) calls: strip variable keys that look like
        # secrets before they can be embedded in the URL, body, headers, or params.
        # Internal helix:// calls receive the full variables — they are trusted
        # operator-configured services running inside the same infrastructure.
        substitution_vars = variables if is_helix_uri else _redact_sensitive(variables)

        # Resolve the URL (variable substitution uses the filtered dict for external calls)
        url = self._resolve_url(impl, substitution_vars)

        # SSRF check — only for direct http/https URLs; helix:// URIs are
        # operator-configured via the service registry and are trusted.
        if not is_helix_uri:
            try:
                await _assert_url_safe(url)
            except _SSRFBlockedError as exc:
                logger.error(
                    "ssrf_blocked",
                    task_id=task.id,
                    url=url,
                    reason=str(exc),
                )
                raise ValueError(
                    f"ServiceTask '{task.name}': URL blocked for security reasons — {exc}"
                ) from exc

        # Build request parameters
        method = extensions.get("helix:method", "POST").upper()
        timeout = float(extensions.get("helix:timeout", self._default_timeout))
        result_var = extensions.get("helix:resultVariable", f"_result_{task.id}")

        # Build headers (use filtered vars for external calls)
        headers = dict(self._default_headers)
        if "helix:headers" in extensions:
            extra_headers = self._parse_json_with_vars(extensions["helix:headers"], substitution_vars)
            if isinstance(extra_headers, dict):
                headers.update(extra_headers)

        # Build body
        body = None
        if "helix:body" in extensions:
            body = self._parse_json_with_vars(extensions["helix:body"], substitution_vars)
        elif method in ("POST", "PUT", "PATCH"):
            if is_helix_uri:
                # Internal helix:// services expect the process variables as context.
                body = variables
            else:
                # External URLs: never send process variables by default.
                # An explicit helix:body extension is required to avoid accidental
                # customer data exfiltration to third-party endpoints.
                body = {}

        # Build query params (use filtered vars for external calls)
        params = None
        if "helix:params" in extensions:
            params = self._parse_json_with_vars(extensions["helix:params"], substitution_vars)

        logger.info("http_resolver_calling",
                     task_id=task.id,
                     method=method,
                     url=url,
                     timeout=timeout)

        # Make the HTTP call
        client = await self._get_client()

        try:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body if method in ("POST", "PUT", "PATCH") else None,
                params=params,
                timeout=timeout,
            )

            # Parse response
            response_data = self._parse_response(response)

            logger.info("http_resolver_success",
                         task_id=task.id,
                         status_code=response.status_code,
                         url=url)

            return {
                result_var: response_data,
                f"_http_status_{task.id}": response.status_code,
            }

        except httpx.TimeoutException:
            logger.error("http_resolver_timeout", task_id=task.id, url=url, timeout=timeout)
            return {
                result_var: None,
                f"_http_status_{task.id}": 0,
                f"_http_error_{task.id}": f"Timeout after {timeout}s",
            }

        except httpx.RequestError as e:
            logger.error("http_resolver_error", task_id=task.id, url=url, error=str(e))
            return {
                result_var: None,
                f"_http_status_{task.id}": 0,
                f"_http_error_{task.id}": str(e),
            }

    # ── URL resolution ────────────────────────────────────────────

    def _resolve_url(self, implementation: str, variables: dict[str, Any]) -> str:
        """
        Resolve an implementation URI to a callable URL.

        - ``https://...`` and ``http://...`` → used as-is (with variable substitution).
        - ``helix://service-name/path`` → looked up in the service registry.
        """
        if implementation.startswith("helix://"):
            # Parse: helix://service-name/path/to/endpoint
            without_scheme = implementation[len("helix://"):]
            parts = without_scheme.split("/", 1)
            service_name = parts[0]
            path = "/" + parts[1] if len(parts) > 1 else ""

            base_url = self._registry.get(service_name)
            if base_url is None:
                raise ValueError(
                    f"Unknown helix:// service '{service_name}'. "
                    f"Registered services: {sorted(self._registry.keys())}. "
                    f"Add it to HELIX_SERVICE_REGISTRY or the HttpTaskResolver constructor."
                )

            url = f"{base_url.rstrip('/')}{path}"
        else:
            url = implementation

        # Variable substitution in the URL
        url = self._substitute_vars(url, variables)
        return url

    # ── Variable substitution ─────────────────────────────────────

    def _substitute_vars(self, template: str, variables: dict[str, Any]) -> str:
        """Replace ``${var_name}`` with variable values."""
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            value = variables.get(var_name, match.group(0))  # Keep original if not found
            return str(value)

        return _VAR_PATTERN.sub(_replace, template)

    def _parse_json_with_vars(self, raw: str, variables: dict[str, Any]) -> Any:
        """
        Parse a JSON string with variable substitution.

        First substitutes ``${var}`` patterns, then parses as JSON.
        If parsing fails, returns the substituted string as-is.
        """
        substituted = self._substitute_vars(raw, variables)
        try:
            return json.loads(substituted)
        except (json.JSONDecodeError, TypeError):
            return substituted

    # ── Response parsing ──────────────────────────────────────────

    @staticmethod
    def _parse_response(response: httpx.Response) -> Any:
        """
        Parse an HTTP response into a Python object.

        Tries JSON first, falls back to text.
        """
        content_type = response.headers.get("content-type", "")

        if "application/json" in content_type:
            try:
                return response.json()
            except Exception:
                pass

        # Try JSON anyway (many APIs don't set content-type correctly)
        try:
            return response.json()
        except Exception:
            pass

        # Fall back to text
        return response.text
