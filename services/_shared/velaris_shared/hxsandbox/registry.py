"""HxSandbox module validation + registry (#17 Phase 2).

Install-time validation of ``runtime: wasm`` guest modules — the #15 mandate
is "install-time failure, never first-run". A module is accepted only if it
compiles, exports the full ABI, and imports nothing outside the granted
capability allowlist (empty in Phase 2).

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

from velaris_shared.hxsandbox.capabilities import GRANTED_CAPABILITIES
from velaris_shared.hxsandbox.host import REQUIRED_EXPORTS, compile_module

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


# ─── Extraction from a `.hxapp` ───────────────────────────────────

#: Where a `runtime: wasm` package keeps its compiled guest, unless the
#: manifest names another path in `wasm_module`.
DEFAULT_MODULE_PATH = "module.wasm"

#: A compiled transform is small. This bound is about refusing a zip bomb
#: before it is read into memory, not about accommodating large guests.
MAX_MODULE_BYTES = 8 * 1024 * 1024


def extract_module(hxapp_bytes: bytes, manifest: dict) -> bytes:
    """Pull the guest module out of a `.hxapp` and validate it.

    Raises :class:`ModuleValidationError` if the bundle carries no module, the
    module is implausibly large, or it fails :func:`validate_module`. The
    caller stores the returned bytes; nothing here touches the database.

    The declared path is resolved against the archive rather than the
    filesystem, and rejected if it tries to escape — a manifest is publisher-
    controlled input.
    """
    import io
    import zipfile

    path = str(manifest.get("wasm_module") or DEFAULT_MODULE_PATH)
    if path.startswith("/") or ".." in path.split("/"):
        raise ModuleValidationError(f"wasm_module path {path!r} is not inside the bundle")

    try:
        with zipfile.ZipFile(io.BytesIO(hxapp_bytes)) as zf:
            if path not in set(zf.namelist()):
                raise ModuleValidationError(
                    f"package declares runtime 'wasm' but carries no {path}")
            info = zf.getinfo(path)
            if info.file_size > MAX_MODULE_BYTES:
                raise ModuleValidationError(
                    f"{path} is {info.file_size} bytes, over the "
                    f"{MAX_MODULE_BYTES}-byte limit")
            wasm_bytes = zf.read(path)
    except zipfile.BadZipFile as exc:
        raise ModuleValidationError(f"package is not a readable archive: {exc}") from exc

    validate_module(wasm_bytes)   # compiles, checks ABI + capabilities
    return wasm_bytes
