"""Enterprise identity — the provisioning spine every entry point calls.

A federated user is not one row. SAML JIT, SCIM and the existing
``/user-directory/bulk-sync`` all mean "make this person exist here", and each
one that writes its own subset of the four tables involved is another way for
them to disagree. :mod:`user_service.identity.provisioning` is the one writer.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from user_service.identity.provisioning import (  # noqa: F401
    ProvisionResult,
    deactivate_principal,
    sync_groups,
    upsert_principal,
)

__all__ = [
    "ProvisionResult",
    "deactivate_principal",
    "sync_groups",
    "upsert_principal",
]
