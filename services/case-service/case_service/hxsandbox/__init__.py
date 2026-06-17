"""HxSandbox (#17) — safe execution of customer-authored code.

Phase 1 (``core.safe_expression``) hardens expression rules. Phase 2 (this
package) is the WASM execution host for ``runtime: wasm`` marketplace
packages, whose first consumer is HxSync custom field-mapping transforms —
pure compute, value-in → value-out, no ambient I/O.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from case_service.hxsandbox.capabilities import (
    GRANTED_CAPABILITIES,
    UngrantedCapabilityError,
    validate_capabilities,
)
from case_service.hxsandbox.host import (
    HxSandboxError,
    SandboxLimits,
    run_transform,
)
from case_service.hxsandbox.registry import (
    ModuleValidationError,
    validate_module,
)

__all__ = [
    "GRANTED_CAPABILITIES",
    "UngrantedCapabilityError",
    "validate_capabilities",
    "HxSandboxError",
    "SandboxLimits",
    "run_transform",
    "ModuleValidationError",
    "validate_module",
]
