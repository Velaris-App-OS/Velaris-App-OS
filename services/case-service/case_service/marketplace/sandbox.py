"""Sandbox container lifecycle management.

Provisions, monitors, and destroys isolated Docker containers for marketplace
sandbox workspaces. Each workspace gets its own container with:
  - Zero database credentials
  - Synthetic data volume (read-only)
  - All egress blocked by default (iptables DROP)
  - Non-root user, read-only filesystem, dropped kernel capabilities
  - seccomp profile applied

Separation of concerns:
  sandbox.py  — container lifecycle (create, destroy, pause, resume)
  network_enforcer.py — iptables rules inside containers
  synthetic_data.py   — fabricated data volume content
  checksum.py         — .hxapp integrity verification
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException

from case_service.marketplace.network_enforcer import apply_default_drop
from case_service.marketplace.synthetic_data import write_dataset_to_file

logger = logging.getLogger(__name__)

# Image used for all sandbox containers.
# Must be present on the host — pulled during platform startup or deployment.
SANDBOX_IMAGE = os.getenv("MARKETPLACE_SANDBOX_IMAGE", "velaris-marketplace-sandbox:latest")

# Resource limits per container
CONTAINER_MEM_LIMIT   = os.getenv("MARKETPLACE_SANDBOX_MEM_LIMIT",   "512m")
CONTAINER_CPU_QUOTA   = int(os.getenv("MARKETPLACE_SANDBOX_CPU_QUOTA", "50000"))   # 50% of one core
CONTAINER_PIDS_LIMIT  = int(os.getenv("MARKETPLACE_SANDBOX_PIDS_LIMIT", "64"))

# Seccomp profile — blocks dangerous syscalls
_SECCOMP_PROFILE_PATH = Path(__file__).parent / "seccomp_sandbox.json"

# Capabilities to DROP from containers (no elevated privileges)
DROPPED_CAPS = [
    "NET_ADMIN", "SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE",
    "SYS_RAWIO", "NET_RAW", "SETUID", "SETGID",
]


def _get_client() -> docker.DockerClient:
    return docker.from_env()


def provision_sandbox(
    workspace_id: str,
    package_ids: list[str],
    synthetic_data_json: dict[str, Any] | None = None,
) -> str:
    """Spin up an isolated sandbox container for a workspace.

    Returns the Docker container ID.
    Raises DockerException on any failure.
    """
    client = _get_client()

    # Write synthetic data to a temp directory (becomes a read-only bind mount)
    data_dir = tempfile.mkdtemp(prefix=f"velaris-sandbox-{workspace_id[:8]}-")
    data_file = os.path.join(data_dir, "synthetic_data.json")

    if synthetic_data_json:
        with open(data_file, "w") as f:
            json.dump(synthetic_data_json, f)
    else:
        write_dataset_to_file(data_file, count=50)

    # Environment — explicitly pass NO database credentials
    env = {
        "SANDBOX_MODE":       "true",
        "WORKSPACE_ID":       workspace_id,
        "PACKAGE_IDS":        ",".join(package_ids),
        "SYNTHETIC_DATA_PATH": "/sandbox/data/synthetic_data.json",
        # DATABASE_URL intentionally omitted — sandbox has zero DB access
    }

    # Seccomp profile
    seccomp_json = None
    if _SECCOMP_PROFILE_PATH.exists():
        with open(_SECCOMP_PROFILE_PATH) as f:
            seccomp_json = f.read()

    security_opt = ["no-new-privileges:true"]
    if seccomp_json:
        security_opt.append(f"seccomp={seccomp_json}")

    container = client.containers.run(
        image=SANDBOX_IMAGE,
        detach=True,
        name=f"velaris-sandbox-{workspace_id[:12]}",
        environment=env,
        # Bind-mount synthetic data as read-only
        volumes={
            data_dir: {"bind": "/sandbox/data", "mode": "ro"},
        },
        # Resource limits
        mem_limit=CONTAINER_MEM_LIMIT,
        cpu_quota=CONTAINER_CPU_QUOTA,
        pids_limit=CONTAINER_PIDS_LIMIT,
        # Security hardening
        user="65534",                    # nobody (non-root)
        read_only=True,                  # read-only filesystem
        tmpfs={"/tmp": "size=64m"},      # writable /tmp only
        cap_drop=DROPPED_CAPS,
        security_opt=security_opt,
        # Network isolation — no access to host network
        network_mode="bridge",           # isolated bridge, no host routes
        # Labels for identification
        labels={
            "velaris.sandbox":      "true",
            "velaris.workspace_id": workspace_id,
        },
    )

    logger.info("Sandbox container %s provisioned for workspace %s", container.id, workspace_id)

    # Apply egress DROP default immediately after start
    try:
        apply_default_drop(container.id)
    except Exception as exc:
        logger.error(
            "Failed to apply egress DROP to container %s — destroying to prevent unsecured access: %s",
            container.id, exc,
        )
        destroy_sandbox(container.id)
        raise RuntimeError(f"Sandbox network hardening failed: {exc}") from exc

    return container.id


def destroy_sandbox(container_id: str) -> None:
    """Stop and remove a sandbox container and its volumes."""
    try:
        client = _get_client()
        container = client.containers.get(container_id)
        container.stop(timeout=5)
        container.remove(v=True, force=True)   # v=True removes anonymous volumes
        logger.info("Sandbox container %s destroyed", container_id)
    except docker.errors.NotFound:
        logger.debug("Container %s already removed", container_id)
    except DockerException as exc:
        logger.error("Failed to destroy container %s: %s", container_id, exc)


def pause_sandbox(container_id: str) -> None:
    """Pause an idle container (freezes CPU — still uses RAM, no disk cost)."""
    try:
        client = _get_client()
        client.containers.get(container_id).pause()
        logger.info("Sandbox container %s paused", container_id)
    except DockerException as exc:
        logger.warning("Could not pause container %s: %s", container_id, exc)


def resume_sandbox(container_id: str) -> None:
    """Resume a paused container."""
    try:
        client = _get_client()
        client.containers.get(container_id).unpause()
        logger.info("Sandbox container %s resumed", container_id)
    except DockerException as exc:
        logger.warning("Could not resume container %s: %s", container_id, exc)


def container_is_running(container_id: str) -> bool:
    """Check if a sandbox container is currently running."""
    try:
        client = _get_client()
        c = client.containers.get(container_id)
        return c.status == "running"
    except (docker.errors.NotFound, DockerException):
        return False


def list_sandbox_containers() -> list[dict[str, str]]:
    """List all live sandbox containers managed by Velaris."""
    try:
        client = _get_client()
        containers = client.containers.list(filters={"label": "velaris.sandbox=true"})
        return [
            {
                "id":           c.id[:12],
                "status":       c.status,
                "workspace_id": c.labels.get("velaris.workspace_id", ""),
                "name":         c.name,
            }
            for c in containers
        ]
    except DockerException as exc:
        logger.error("Failed to list sandbox containers: %s", exc)
        return []
