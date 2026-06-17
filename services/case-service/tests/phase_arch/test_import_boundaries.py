"""Modular-boundary guard (roadmap #13 — keep service extraction cheap).

Service extraction (#13b-e) is DEFERRED until a real driver appears (scaling,
fault isolation, deploy cadence, a team). Deferral only stays cheap if the
module boundaries don't drift in the meantime. These tests enforce exactly
that, so a future split remains a 1-2 day job instead of a rewrite:

* **Rule 1 — shared-layer purity.** The would-be `velaris_shared` layer
  (config / db / auth / hxguard) must NOT import feature modules. This keeps
  it liftable into a standalone package (13b) at any time. Hard rule.

* **Rule 2 — extraction-target coupling ratchet.** Each candidate service
  (analytics, process-mining, graph/hxgraph, hxmigrate) may only reach into
  case_service via the shared layer, its own package, and a recorded baseline
  of known cross-deps (to be bundled/resolved at extraction time). Any NEW
  cross-module import fails the test — drift is caught the moment it's added.

See docs/Future/tier2-sequence-and-service-extraction.md.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ast
import pathlib

import pytest

CS_ROOT = pathlib.Path(__file__).resolve().parents[2] / "case_service"
_PREFIX = "case_service."

# The would-be velaris_shared layer.
SHARED = {"config", "db", "auth", "hxguard"}

# config/db/auth are genuinely pure today and must stay that way (hard rule).
SHARED_PURE = {"config", "db", "auth"}

# hxguard is shared too, but carries ONE recorded coupling today:
# hxguard/service.py -> enterprise.security_events (the deny-audit SecurityEvent
# sink). Recorded as debt to resolve at 13b (move the SecurityEvent model/writer
# into the shared db layer, or inject the sink) — not allowed to grow.
HXGUARD_ALLOWED_EXTERNAL = {"enterprise"}


def _case_service_imports(pyfile: pathlib.Path) -> set[str]:
    """Dotted case_service.* module paths imported by *pyfile* (prefix stripped)."""
    tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Only absolute case_service imports (level 0); relative imports stay
            # inside the package and are not cross-module concerns.
            if node.level == 0 and node.module and node.module.startswith(_PREFIX):
                out.add(node.module[len(_PREFIX):])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_PREFIX):
                    out.add(alias.name[len(_PREFIX):])
    return out


def _top(modpath: str) -> str:
    return modpath.split(".")[0] if modpath else ""


def _iter_py(*paths: pathlib.Path):
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            yield from p.rglob("*.py")


# ─── Rule 1 — shared layer must be standalone ─────────────────────

def test_shared_core_does_not_import_feature_modules():
    """config/db/auth must import nothing outside the shared layer (hard rule)."""
    core_paths = [CS_ROOT / "config.py", CS_ROOT / "db", CS_ROOT / "auth"]
    violations: list[str] = []
    for f in _iter_py(*core_paths):
        for imp in _case_service_imports(f):
            if _top(imp) not in SHARED:
                violations.append(f"  {f.relative_to(CS_ROOT)}  ->  case_service.{imp}")
    assert not violations, (
        "Shared core (config/db/auth) must not import feature modules — this is "
        "what keeps velaris_shared liftable. Offenders:\n" + "\n".join(sorted(violations))
    )


def test_hxguard_coupling_does_not_grow():
    """hxguard may reach outside shared only via its recorded baseline."""
    external: set[str] = set()
    for f in _iter_py(CS_ROOT / "hxguard"):
        for imp in _case_service_imports(f):
            t = _top(imp)
            if t and t not in SHARED:
                external.add(t)
    new = external - HXGUARD_ALLOWED_EXTERNAL
    assert not new, (
        f"hxguard gained NEW cross-module coupling: {sorted(new)}. hxguard is part of the "
        f"shared layer; new feature-module deps make velaris_shared harder to lift. Avoid it, "
        f"or add to HXGUARD_ALLOWED_EXTERNAL as reviewed debt."
    )


# ─── Rule 2 — extraction targets must not grow new coupling ───────
# Baselines are the cross-module deps that exist TODAY (to be bundled/resolved
# at extraction time). The test fails if a target gains a NEW external dep.

TARGETS: dict[str, dict] = {
    "analytics": {
        "own": {"analytics"},
        "router": "api/routers/analytics.py",
        "allowed_external": {"hxnexus"},
    },
    "process_mining": {
        "own": {"process_mining"},
        "router": "api/routers/process_mining.py",
        "allowed_external": set(),
    },
    "graph": {
        "own": {"hxgraph"},
        "router": "api/routers/graph.py",
        # hxgraph today reads api.routers.sitemap (module registry) + hxnexus.factory.
        "allowed_external": {"api", "hxnexus"},
    },
    "hxmigrate": {
        "own": {"hxmigrate"},
        "router": "api/routers/hxmigrate.py",
        "allowed_external": {"bpm_importer", "hxnexus", "hxstream", "orchestrator", "scout"},
    },
}


@pytest.mark.parametrize("name", list(TARGETS))
def test_extraction_target_coupling_does_not_grow(name):
    spec = TARGETS[name]
    paths = [CS_ROOT / spec["router"]] + [CS_ROOT / o for o in spec["own"]]
    external: set[str] = set()
    for f in _iter_py(*paths):
        for imp in _case_service_imports(f):
            t = _top(imp)
            if t and t not in SHARED and t not in spec["own"]:
                external.add(t)
    new = external - spec["allowed_external"]
    assert not new, (
        f"Extraction target '{name}' gained NEW cross-module coupling: {sorted(new)}.\n"
        f"This makes a future service split harder. Either avoid the import, or — if it is "
        f"genuinely required — add it to TARGETS['{name}']['allowed_external'] (a deliberate, "
        f"reviewed decision recording another dep to bundle at extraction)."
    )
