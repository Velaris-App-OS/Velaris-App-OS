"""Marketplace Layer-2 — production app-container runtime (mig 124).

The publisher's image runs on Velaris infra as its own isolated program:
digest-pinned identity, full sandbox hardening posture (non-root, read-only
filesystem, dropped capabilities, seccomp, no-new-privileges, resource caps),
egress-DROP plus exactly the granted domains, and ZERO database credentials —
the scoped broker (`/api/v1/broker`, P2) is the only data path. A container
starts only when its capability grant is granted and stops the instant the
grant is revoked.

Separation of concerns mirrors the sandbox:
  runtime.py          — production app-container lifecycle
  sandbox.py          — preview/workspace containers (synthetic data)
  network_enforcer.py — iptables rules inside containers
  checksum.py         — manifest/image declaration validation

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timezone

from case_service.config import get_settings
from case_service.marketplace.sandbox import (
    DROPPED_CAPS,
    _SECCOMP_PROFILE_PATH,
    _get_client,
)

logger = logging.getLogger(__name__)

CONTAINER_PIDS_LIMIT = 64

# The apps network is INTERNAL: containers on it have no route out of the
# host at all — egress-DROP by construction, with no dependency on iptables
# (or root) inside arbitrary publisher images. The only reachable service is
# the broker gateway container, which exposes exactly /api/v1/broker.
APPS_NETWORK = "velaris-apps"
BROKER_URL_IN_NETWORK = "http://velaris-broker-gw/api/v1/broker"


class Layer2Error(ValueError):
    """Provisioning refused or failed — always fail-closed. Subclasses
    ValueError so router-level `except ValueError` surfaces it as a 400."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def registry_of(image: str) -> str:
    """The registry host of an image reference. `nginx` and `library/nginx`
    style references resolve to docker.io."""
    first = image.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first.split(":", 1)[0] if not first.startswith("localhost") else "localhost"
    return "docker.io"


def check_registry_allowed(image: str) -> str:
    """Fail-closed registry allowlist from platform config (never the manifest)."""
    allowed = [r.strip() for r in
               (get_settings().marketplace_l2_registries or "").split(",") if r.strip()]
    registry = registry_of(image)
    if not allowed:
        raise Layer2Error(
            "No Layer-2 registries are allowed on this platform "
            "(VELARIS_CASE_MARKETPLACE_L2_REGISTRIES is empty) — install refused.")
    if registry not in allowed:
        raise Layer2Error(
            f"Image registry '{registry}' is not in the platform allowlist "
            f"({', '.join(allowed)}) — install refused.")
    return registry


def verify_image_signature(image: str, digest: str) -> bool:
    """Cosign verification, governed by platform policy.

    - policy OFF  → returns False (recorded as unverified, never blocks).
    - policy ON   → cosign must be installed AND verify successfully,
                    otherwise provisioning fails closed.
    """
    require = get_settings().marketplace_l2_require_signature
    cosign = shutil.which("cosign")
    if not require:
        return False
    if not cosign:
        raise Layer2Error(
            "Signature verification is required but the cosign binary is not "
            "installed on this host — provisioning refused.")
    ref = f"{image}@{digest}"
    result = subprocess.run(
        [cosign, "verify", ref], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise Layer2Error(
            f"cosign verification FAILED for {ref}: {result.stderr[:300]}")
    return True


def pull_pinned_image(image: str, digest: str) -> None:
    """Pull by DIGEST — the tag is display-only and never trusted."""
    client = _get_client()
    ref = f"{image.split(':', 1)[0]}@{digest}"
    logger.info("Layer-2 pulling digest-pinned image %s", ref)
    client.images.pull(ref)


def ensure_apps_network() -> None:
    """Create the internal apps network if compose hasn't yet."""
    client = _get_client()
    try:
        client.networks.get(APPS_NETWORK)
    except Exception:
        client.networks.create(APPS_NETWORK, driver="bridge", internal=True,
                               labels={"velaris.apps": "true"})
        logger.info("Created internal docker network %s", APPS_NETWORK)


def provision_app_container(
    *,
    row_id: str,
    tenant_id: str,
    package_id: str,
    image: str,
    digest: str,
    granted_domains: list[str],
    broker_url: str | None = None,
    broker_token: str | None = None,
    declared_env: dict | None = None,
    declared_command: list[str] | None = None,
    port: int | None = None,
) -> str:
    """Start the publisher's container with the full hardening posture on the
    INTERNAL apps network (egress-DROP by construction — no route out).

    External outbound domains are refused fail-closed in this release:
    granting them would silently not be enforceable without a host-privileged
    packet filter. The broker is the container's only reach.

    Returns the docker container id. Every failure raises Layer2Error.
    """
    if granted_domains:
        raise Layer2Error(
            "Layer-2 containers run fully egress-isolated in this release — "
            "external outbound domains cannot be granted to a container yet. "
            "Approve with no outbound domains (the scoped broker is the data path).")
    client = _get_client()
    settings = get_settings()
    ensure_apps_network()
    ref = f"{image.split(':', 1)[0]}@{digest}"

    seccomp_json = None
    if _SECCOMP_PROFILE_PATH.exists():
        seccomp_json = _SECCOMP_PROFILE_PATH.read_text()
    security_opt = ["no-new-privileges:true"]
    if seccomp_json:
        security_opt.append(f"seccomp={seccomp_json}")

    env = dict(declared_env or {})
    env.update({
        "VELARIS_APP": "true",
        "VELARIS_TENANT": tenant_id,
        "VELARIS_PACKAGE": package_id,
        # DATABASE_URL and every platform secret intentionally absent —
        # the broker is the only data path.
    })
    if broker_url:
        env["VELARIS_BROKER_URL"] = broker_url
    if broker_token:
        env["VELARIS_BROKER_TOKEN"] = broker_token

    container = client.containers.run(
        image=ref,
        detach=True,
        name=f"velaris-app-{row_id[:12]}",
        command=declared_command,        # manifest-declared, admin-reviewed
        environment=env,
        mem_limit=settings.marketplace_l2_mem_limit,
        cpu_quota=settings.marketplace_l2_cpu_quota,
        pids_limit=CONTAINER_PIDS_LIMIT,
        user="65534",
        read_only=True,
        tmpfs={"/tmp": "size=64m"},
        cap_drop=DROPPED_CAPS,
        security_opt=security_opt,
        network=APPS_NETWORK,
        labels={
            "velaris.app":        "true",
            "velaris.tenant":     tenant_id,
            "velaris.package":    package_id,
            "velaris.row_id":     row_id,
        },
    )
    logger.info("Layer-2 app container %s running for %s/%s (%s) on %s",
                container.id[:12], tenant_id, package_id, ref, APPS_NETWORK)
    return container.id


def stop_app_container(container_id: str) -> None:
    """Stop + remove — used for revocation (instant) and teardown."""
    try:
        client = _get_client()
        container = client.containers.get(container_id)
        container.stop(timeout=5)
        container.remove(force=True)
        logger.info("Layer-2 app container %s stopped and removed", container_id[:12])
    except Exception as exc:
        logger.warning("Layer-2 container %s teardown: %s", container_id[:12], exc)


def container_running(container_id: str) -> bool:
    try:
        client = _get_client()
        return client.containers.get(container_id).status == "running"
    except Exception:
        return False
