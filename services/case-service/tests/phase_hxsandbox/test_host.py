"""HxSandbox #17 Phase 2 — WASM host isolation + metering tests.

Exercises the security-critical contract of the Wasmtime host directly, with
hand-written ``.wat`` fixtures (no external wasm package needed):

* identity / constant round-trips (the ABI works),
* fuel exhaustion, wall-clock (epoch), and memory caps all trap,
* the empty capability allowlist rejects any imported host function,
* missing ABI exports, oversized output, OOB pointers, and invalid JSON
  output are all rejected.

Timing is never asserted — only that the expected trap/rejection is raised.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from case_service.hxsandbox import (
    HxSandboxError,
    ModuleValidationError,
    SandboxLimits,
    run_transform,
    validate_module,
)
from case_service.hxsandbox.capabilities import (
    UngrantedCapabilityError,
    validate_capabilities,
)

# ─── .wat fixtures ────────────────────────────────────────────────

# Echo: returns the input buffer unchanged → identity transform.
ECHO = b"""(module
  (memory (export "memory") 1)
  (global $bump (mut i32) (i32.const 1024))
  (func (export "hx_alloc") (param $n i32) (result i32)
    (local $p i32)
    global.get $bump local.set $p
    global.get $bump local.get $n i32.add global.set $bump
    local.get $p)
  (func (export "hx_transform") (param $ptr i32) (param $len i32) (result i64)
    local.get $ptr i64.extend_i32_u i64.const 32 i64.shl
    local.get $len i64.extend_i32_u i64.or))"""

# Constant: ignores input, returns {"value":"xf"} (14 bytes) from a data seg.
CONST = b"""(module
  (memory (export "memory") 1)
  (data (i32.const 2048) "{\\"value\\":\\"xf\\"}")
  (func (export "hx_alloc") (param i32) (result i32) i32.const 4096)
  (func (export "hx_transform") (param i32 i32) (result i64)
    i64.const 2048 i64.const 32 i64.shl i64.const 14 i64.or))"""

# Spin: infinite loop in hx_transform.
SPIN = b"""(module (memory (export "memory") 1)
  (func (export "hx_alloc") (param i32) (result i32) i32.const 0)
  (func (export "hx_transform") (param i32 i32) (result i64) (loop br 0) i64.const 0))"""

# Importer: imports a host function (must be rejected — empty allowlist).
IMPORTER = b"""(module
  (import "env" "secret" (func))
  (memory (export "memory") 1)
  (func (export "hx_alloc") (param i32) (result i32) i32.const 0)
  (func (export "hx_transform") (param i32 i32) (result i64) i64.const 0))"""

# Missing the hx_transform export.
MISSING_EXPORT = b"""(module (memory (export "memory") 1)
  (func (export "hx_alloc") (param i32) (result i32) i32.const 0))"""

# Declares 100 pages (6.4 MB) of initial memory — exceeds a small cap.
BIG_MEMORY = b"""(module (memory (export "memory") 100)
  (func (export "hx_alloc") (param i32) (result i32) i32.const 1024)
  (func (export "hx_transform") (param i32 i32) (result i64) i64.const 0))"""

# Claims a 5 MB output via the packed length.
OVERSIZE = b"""(module (memory (export "memory") 1)
  (func (export "hx_alloc") (param i32) (result i32) i32.const 0)
  (func (export "hx_transform") (param i32 i32) (result i64)
    i64.const 0 i64.const 32 i64.shl i64.const 5000000 i64.or))"""

# Returns an out-of-bounds pointer (far past linear memory).
OOB = b"""(module (memory (export "memory") 1)
  (func (export "hx_alloc") (param i32) (result i32) i32.const 0)
  (func (export "hx_transform") (param i32 i32) (result i64)
    i64.const 4000000 i64.const 32 i64.shl i64.const 8 i64.or))"""

# Returns non-JSON bytes.
BAD_JSON = b"""(module (memory (export "memory") 1)
  (data (i32.const 16) "not json")
  (func (export "hx_alloc") (param i32) (result i32) i32.const 1024)
  (func (export "hx_transform") (param i32 i32) (result i64)
    i64.const 16 i64.const 32 i64.shl i64.const 8 i64.or))"""


# ─── ABI round-trips ──────────────────────────────────────────────

@pytest.mark.parametrize("value", [42, "hello", [1, 2, 3], {"a": 1}, 3.5, True, None])
def test_echo_identity(value):
    assert run_transform(ECHO, value) == value


def test_constant_transform_reads_guest_output():
    assert run_transform(CONST, "anything") == "xf"


# ─── Metering / isolation traps ───────────────────────────────────

def test_fuel_exhaustion_traps():
    with pytest.raises(HxSandboxError):
        run_transform(SPIN, 1, SandboxLimits(fuel=100_000))


def test_wall_clock_epoch_traps():
    # Huge fuel so this can only be stopped by the epoch deadline.
    with pytest.raises(HxSandboxError):
        run_transform(SPIN, 1, SandboxLimits(fuel=10**12, epoch_ticks=2))


def test_memory_cap_rejects_oversized_module():
    with pytest.raises(HxSandboxError):
        run_transform(BIG_MEMORY, 1, SandboxLimits(memory_bytes=1024 * 1024))


# ─── ABI / output violations ──────────────────────────────────────

def test_oversized_output_rejected():
    with pytest.raises(HxSandboxError):
        run_transform(OVERSIZE, 1, SandboxLimits(max_output_bytes=1024 * 1024))


def test_out_of_bounds_pointer_rejected():
    with pytest.raises(HxSandboxError):
        run_transform(OOB, 1)


def test_invalid_json_output_rejected():
    with pytest.raises(HxSandboxError):
        run_transform(BAD_JSON, 1)


def test_imported_function_rejected_at_runtime():
    with pytest.raises(HxSandboxError):
        run_transform(IMPORTER, 1)


def test_missing_export_rejected_at_runtime():
    with pytest.raises(HxSandboxError):
        run_transform(MISSING_EXPORT, 1)


# ─── Install-time validation ──────────────────────────────────────

def test_validate_accepts_good_module():
    validate_module(ECHO)  # must not raise


def test_validate_rejects_imported_capability():
    with pytest.raises(ModuleValidationError):
        validate_module(IMPORTER)


def test_validate_rejects_missing_export():
    with pytest.raises(ModuleValidationError):
        validate_module(MISSING_EXPORT)


def test_validate_rejects_malformed_wasm():
    with pytest.raises(ModuleValidationError):
        validate_module(b"\x00not-wasm")


# ─── Capability allowlist ─────────────────────────────────────────

def test_empty_capabilities_ok():
    validate_capabilities([])
    validate_capabilities(None)


def test_nonempty_capabilities_rejected():
    with pytest.raises(UngrantedCapabilityError):
        validate_capabilities(["http.fetch"])
