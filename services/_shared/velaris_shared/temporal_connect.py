"""Where Temporal is, and how to reach it — one answer for every client.

Both Temporal clients (case-service's embedded worker and the engine's) read
the address from here.

Address: ``TEMPORAL_HOST`` first — what both clients have always read and what
the Helm charts set — then ``VELARIS_CASE_TEMPORAL_HOST``, then the bundled
``localhost:7233``. Until November 2026 the second was a documented setting
that nothing read; an install that never set either connects exactly where it
did before.

TLS: mutual TLS when ``VELARIS_CASE_TEMPORAL_TLS_{CA,CERT,KEY}`` are set (a
Temporal on another server, VELARIS_EXTERNAL_SERVICES=temporal). All empty =
plaintext, as before. Half set is an error, raised at connect: a client
certificate without its key, or a key without the CA to check the server,
would otherwise quietly connect less safely than its operator configured.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _setting(name: str, default: str = "") -> str:
    """VELARIS_CASE_<NAME> from the process environment, else the settings
    object (which also reads .env). The environment comes first so a process
    whose settings cannot load (a pod given only these variables) still finds
    Temporal — this is called where a failure means "no durable timers"."""
    v = os.environ.get(f"VELARIS_CASE_{name.upper()}")
    if v is not None:
        return v
    try:
        from velaris_shared.config import get_settings

        return getattr(get_settings(), name) or default
    except Exception:
        return default


def temporal_target() -> str:
    return os.environ.get("TEMPORAL_HOST") or _setting("temporal_host") or "localhost:7233"


def temporal_namespace() -> str:
    return os.environ.get("TEMPORAL_NAMESPACE") or _setting("temporal_namespace") or "default"


def temporal_tls() -> Any:
    """``TLSConfig`` for ``Client.connect(tls=…)``, or ``False`` (plaintext)."""
    ca, cert, key = (_setting("temporal_tls_ca"), _setting("temporal_tls_cert"),
                     _setting("temporal_tls_key"))
    if not (ca or cert or key):
        return False
    if not ca or bool(cert) != bool(key):
        raise ValueError(
            "Temporal TLS is half-configured: set VELARIS_CASE_TEMPORAL_TLS_CA, and "
            "VELARIS_CASE_TEMPORAL_TLS_CERT with VELARIS_CASE_TEMPORAL_TLS_KEY"
        )
    from temporalio.service import TLSConfig

    return TLSConfig(
        server_root_ca_cert=Path(ca).read_bytes(),
        client_cert=Path(cert).read_bytes() if cert else None,
        client_private_key=Path(key).read_bytes() if key else None,
        domain=_setting("temporal_tls_server_name") or None,
    )


async def connect(host: str | None = None, namespace: str | None = None):
    """``Client.connect`` with the address, namespace and TLS above."""
    from temporalio.client import Client

    return await Client.connect(
        host or temporal_target(),
        namespace=namespace or temporal_namespace(),
        tls=temporal_tls(),
    )
