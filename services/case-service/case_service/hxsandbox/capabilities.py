"""HxSandbox capability allowlist (#17 Phase 2).

A capability is a named host function a guest module is permitted to import
(e.g. a future ``http.fetch`` or ``kv.get``). The escape route the
architecture report names is ambient I/O — DB/HTTP must never be reachable
unless explicitly granted. Phase 2 therefore ships with an **empty** set of
granted capabilities: the first consumer (HxSync transforms) is pure compute,
so no host function is bound, and a guest that imports anything fails to
instantiate.

The registry is the seam for future scoped capabilities; until a capability
is actually *implemented* and security-reviewed, declaring one is rejected at
install time, so the allowlist can never reference a binding that does not
exist.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

# Capabilities with a real, security-reviewed host-function binding.
# INTENTIONALLY EMPTY in Phase 2 — see module docstring.
GRANTED_CAPABILITIES: frozenset[str] = frozenset()


class UngrantedCapabilityError(Exception):
    """A package declared a capability the platform does not grant."""


def validate_capabilities(declared: list[str] | None) -> None:
    """Reject any declared capability not in :data:`GRANTED_CAPABILITIES`.

    Called at install time. With an empty grant set this means *any*
    non-empty ``capabilities`` list is rejected — keeping Phase 2 airtight.
    """
    for cap in declared or []:
        if cap not in GRANTED_CAPABILITIES:
            raise UngrantedCapabilityError(
                f"capability '{cap}' is not granted by this platform"
            )
