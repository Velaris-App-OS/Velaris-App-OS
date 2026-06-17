"""HxSandbox module validation + registry (#17 Phase 2).

Install-time validation of ``runtime: wasm`` guest modules — the #15 mandate
is "install-time failure, never first-run". A module is accepted only if it
compiles, exports the full ABI, and imports nothing outside the granted
capability allowlist (empty in Phase 2).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

from case_service.hxsandbox.capabilities import GRANTED_CAPABILITIES
from case_service.hxsandbox.host import REQUIRED_EXPORTS, compile_module

logger = logging.getLogger(__name__)


class ModuleValidationError(Exception):
    """A guest module is unfit to install."""


def validate_module(wasm_bytes: bytes) -> None:
    """Validate one guest module at install time.

    Raises :class:`ModuleValidationError` if the module fails to compile, is
    missing an ABI export, or imports a host function the platform does not
    grant. Returns ``None`` on success.
    """
    try:
        module = compile_module(wasm_bytes)
    except Exception as e:
        raise ModuleValidationError(f"module does not compile: {e}") from e

    export_names = {e.name for e in module.exports}
    missing = [n for n in REQUIRED_EXPORTS if n not in export_names]
    if missing:
        raise ModuleValidationError(
            f"module is missing required export(s): {', '.join(missing)}"
        )

    # Every import must map to a granted capability. With an empty grant set
    # this means the module must import nothing at all.
    for imp in module.imports:
        cap = f"{imp.module}.{imp.name}"
        if cap not in GRANTED_CAPABILITIES:
            raise ModuleValidationError(
                f"module imports ungranted capability '{cap}'"
            )
