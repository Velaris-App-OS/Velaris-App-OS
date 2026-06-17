"""HxSandbox WASM execution host (#17 Phase 2).

Embeds Wasmtime to run customer-authored ``runtime: wasm`` modules with hard
resource limits and zero ambient I/O. The first (and only Phase-2) consumer is
HxSync custom field-mapping transforms: a pure ``value -> value`` function.

Isolation guarantees
--------------------
* **No ambient I/O.** Modules are instantiated against an empty ``Linker`` — no
  WASI, no host functions — so a guest can import nothing (filesystem, clock,
  randomness, env, network are all absent). Any import fails instantiation.
* **CPU metered.** Fuel is consumed per instruction; exhaustion traps.
* **Memory bounded.** ``StoreLimits`` caps linear-memory growth.
* **Wall-clock bounded.** A background ticker increments the engine epoch; an
  epoch deadline traps even a non-allocating busy loop that never burns fuel
  fast enough.
* **No cross-call state.** A fresh ``Store`` per call; only the compiled
  ``Module`` is reused.

Guest ABI (JSON over linear memory)
-----------------------------------
The module must export ``memory``, ``hx_alloc(i32) -> i32`` and
``hx_transform(ptr: i32, len: i32) -> i64``. The host writes ``{"value": …}``
to ``hx_alloc``'d memory, calls ``hx_transform``, and reads ``{"value": …}``
back from the returned ``out_ptr<<32 | out_len`` packed pointer.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ABI export names the guest must provide.
EXPORT_MEMORY = "memory"
EXPORT_ALLOC = "hx_alloc"
EXPORT_TRANSFORM = "hx_transform"
REQUIRED_EXPORTS = (EXPORT_MEMORY, EXPORT_ALLOC, EXPORT_TRANSFORM)

# Background epoch ticker cadence. Wall-clock budget = epoch_ticks * this.
_EPOCH_TICK_SECONDS = 0.05


class HxSandboxError(Exception):
    """A sandboxed execution failed (trap, limit hit, or ABI violation)."""


@dataclass(frozen=True)
class SandboxLimits:
    """Per-call resource budget."""

    fuel: int = 50_000_000           # CPU instructions
    memory_bytes: int = 16 * 1024 * 1024
    epoch_ticks: int = 20            # ~1s wall-clock at the default cadence
    max_output_bytes: int = 1024 * 1024

    @property
    def wall_clock_seconds(self) -> float:
        return self.epoch_ticks * _EPOCH_TICK_SECONDS


# ─── Lazily-built shared engine + epoch ticker ────────────────────
# Wasmtime is an optional-at-import dependency: importing this module must not
# fail if the wheel is absent (e.g. a deploy that never enables wasm). The
# engine is built on first use.

_engine = None
_engine_lock = threading.Lock()
_ticker_started = False


def _get_engine():
    global _engine, _ticker_started
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            from wasmtime import Config, Engine
        except ImportError as e:  # pragma: no cover - deploy without wasm
            raise HxSandboxError("wasmtime is not installed on this platform") from e
        config = Config()
        config.consume_fuel = True
        config.epoch_interruption = True
        # Belt-and-braces: no threads/SIMD bloat, deterministic single thread.
        engine = Engine(config)

        if not _ticker_started:
            ticker = threading.Thread(
                target=_epoch_ticker, args=(engine,), daemon=True,
                name="hxsandbox-epoch",
            )
            ticker.start()
            _ticker_started = True
        _engine = engine
        return _engine


def _epoch_ticker(engine) -> None:
    import time
    while True:
        time.sleep(_EPOCH_TICK_SECONDS)
        engine.increment_epoch()


# ─── Module compilation (cached) ──────────────────────────────────

_module_cache: dict[bytes, Any] = {}
_cache_lock = threading.Lock()


def compile_module(wasm_bytes: bytes):
    """Compile (and cache) a guest module from its bytes.

    Accepts ``.wasm`` binary or ``.wat`` text. Raises :class:`HxSandboxError`
    on malformed input — callers validate at install time so this never
    surfaces at first run.
    """
    import hashlib
    key = hashlib.sha256(wasm_bytes).digest()
    cached = _module_cache.get(key)
    if cached is not None:
        return cached
    engine = _get_engine()
    try:
        from wasmtime import Module, wat2wasm
        data = wasm_bytes
        # Allow .wat text fixtures/packages transparently.
        stripped = wasm_bytes.lstrip()
        if stripped[:1] in (b"(", b";"):
            data = wat2wasm(wasm_bytes)
        module = Module(engine, data)
    except Exception as e:
        raise HxSandboxError(f"module failed to compile: {e}") from e
    with _cache_lock:
        _module_cache[key] = module
    return module


# ─── Execution ────────────────────────────────────────────────────


def run_transform(
    wasm_bytes: bytes, value: Any, limits: SandboxLimits | None = None,
) -> Any:
    """Run a guest transform on *value* and return the transformed value.

    Raises :class:`HxSandboxError` on any trap, limit breach, or ABI
    violation. Callers (e.g. HxSync) decide the fail-safe policy — typically
    keep the original value.
    """
    limits = limits or SandboxLimits()
    module = compile_module(wasm_bytes)
    engine = _get_engine()
    from wasmtime import Linker, Store

    store = Store(engine)
    store.set_fuel(limits.fuel)
    store.set_limits(memory_size=limits.memory_bytes)
    store.set_epoch_deadline(limits.epoch_ticks)

    # Empty linker → no host functions → guest can import nothing.
    linker = Linker(engine)
    try:
        instance = linker.instantiate(store, module)
    except Exception as e:
        raise HxSandboxError(f"module instantiation failed: {e}") from e

    exports = instance.exports(store)
    for name in REQUIRED_EXPORTS:
        if name not in exports:
            raise HxSandboxError(f"module missing required export '{name}'")
    memory = exports[EXPORT_MEMORY]
    alloc = exports[EXPORT_ALLOC]
    transform = exports[EXPORT_TRANSFORM]

    payload = json.dumps({"value": value}, separators=(",", ":")).encode("utf-8")

    try:
        ptr = alloc(store, len(payload))
        if not isinstance(ptr, int) or ptr < 0:
            raise HxSandboxError("hx_alloc returned an invalid pointer")
        _bounds_check(memory, store, ptr, len(payload))
        memory.write(store, payload, ptr)

        packed = transform(store, ptr, len(payload))
        out_ptr = (packed >> 32) & 0xFFFFFFFF
        out_len = packed & 0xFFFFFFFF
        if out_len > limits.max_output_bytes:
            raise HxSandboxError(f"output too large ({out_len} bytes)")
        _bounds_check(memory, store, out_ptr, out_len)
        out_bytes = memory.read(store, out_ptr, out_ptr + out_len)
    except HxSandboxError:
        raise
    except Exception as e:
        # Wasmtime traps (fuel/epoch/oob) land here.
        raise HxSandboxError(f"sandboxed execution trapped: {e}") from e

    try:
        decoded = json.loads(bytes(out_bytes))
    except Exception as e:
        raise HxSandboxError(f"module returned invalid JSON: {e}") from e
    if not isinstance(decoded, dict) or "value" not in decoded:
        raise HxSandboxError("module output missing 'value' field")
    return decoded["value"]


def _bounds_check(memory, store, ptr: int, length: int) -> None:
    size = memory.data_len(store)
    if ptr < 0 or length < 0 or ptr + length > size:
        raise HxSandboxError(
            f"memory access out of bounds (ptr={ptr}, len={length}, size={size})"
        )
