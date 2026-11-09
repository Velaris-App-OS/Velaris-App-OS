"""
Temporal Client
================

Manages the connection to the Temporal server.

The client is created once during FastAPI lifespan and shared across
the application.  All workflow operations (start, query, cancel, signal)
go through this client.

Usage::

    from helix_engine.temporal.client import get_client, connect

    # During startup:
    client = await connect()

    # Anywhere else:
    client = get_client()
    handle = await client.start_workflow(...)

Configuration via environment variables::

    TEMPORAL_HOST       then VELARIS_CASE_TEMPORAL_HOST, default: localhost:7233
    TEMPORAL_NAMESPACE  default: default
    VELARIS_CASE_TEMPORAL_TLS_{CA,CERT,KEY}   mutual TLS; unset = plaintext

(velaris_shared.temporal_connect — one answer for both Temporal clients.)
"""

from __future__ import annotations

import os

import structlog
from temporalio.client import Client

logger = structlog.get_logger()

# ── Module-level singleton ────────────────────────────────────────────
_client: Client | None = None


async def connect(
    host: str | None = None,
    namespace: str | None = None,
) -> Client:
    """
    Connect to the Temporal server and store the client as a singleton.

    Args:
        host: Temporal server address (default: TEMPORAL_HOST env or localhost:7233).
        namespace: Temporal namespace (default: TEMPORAL_NAMESPACE env or "default").

    Returns:
        Connected Temporal client.
    """
    global _client

    from velaris_shared import temporal_connect as tc

    host = host or tc.temporal_target()
    namespace = namespace or tc.temporal_namespace()

    logger.info("temporal_connecting", host=host, namespace=namespace)

    _client = await Client.connect(
        host,
        namespace=namespace,
        tls=tc.temporal_tls(),
    )

    logger.info("temporal_connected", host=host, namespace=namespace)
    return _client


def get_client() -> Client:
    """
    Get the shared Temporal client.

    Raises:
        RuntimeError: If ``connect()`` has not been called yet.
    """
    if _client is None:
        raise RuntimeError(
            "Temporal client not connected. "
            "Call `await connect()` during application startup."
        )
    return _client


def is_connected() -> bool:
    """Check if the Temporal client has been initialised."""
    return _client is not None
