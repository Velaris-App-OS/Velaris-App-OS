"""The role-name authorization still living inside handler bodies.

Phase 3 migrated every *declarative* gate — the ones written as a FastAPI
dependency. It never touched the *imperative* ones: a helper called in the body
of a handler that reads `user.is_admin`, `user.roles` or `has_role(...)` and
raises 403 itself. There are more of those than there were gates.

**Why this module exists rather than a doc section.** The count was wrong three
times, each time because the detector was narrower than the thing it counted:

  1. `require_role(` missed the 12 router-level `Depends(...)` gates.
  2. It also missed `require_admin()`, a one-line alias — 56 more.
  3. An AST pass for `user.is_admin` missed `hxdbmigrate._require_admin`, whose
     body reads a *local* `roles` variable, and the ten handlers calling it.

The pattern is always the same: a new spelling appears, the old detector does
not know about it, and the gap is invisible until something depends on it being
zero. Phase 4 was exactly that something — it deleted `has_role` and demoted
`is_admin` to a display flag, so anything still reading them for a decision
would have changed behaviour silently.

**Step 2 (2026-08-15) migrated the population: 205 sites → 75.** The remainder
is deliberate and is not role-name authorization — superadmin identity tests,
display serialization, one literal role match that exists *because* role
inheritance was the bug, and case-level `has_privilege` calls. The four kinds
are set out in the header of `legacy_sites.txt`. This scanner cannot tell them
apart from a real decision (they all read the identity model), which is exactly
why the ledger is a reviewed file rather than a threshold.

So the detector lives in code, runs in the test suite, and is compared against
a recorded expectation. A new site fails the test. A migrated site fails the
test. Either way somebody has to look.

**What counts as a signal**: a read of the identity model itself — `is_admin`,
`is_designer`, `is_superadmin`, `roles`, `has_role()`, `has_privilege()` — and,
transitively, any router-local function that calls one. Feature flags
(`_require_flag`, `is_feature_enabled`, `require_enabled_user`) are not signals:
they decide whether a module is switched on, not who may use it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: Every router directory on the platform, not just this service's.
#:
#: 13c-e moved seven routers into analytics-service, hxgraph-service and
#: hxmigrate-service. A scanner that kept looking only here would have gone on
#: reporting the same clean number while an imperative role check could be
#: added in an extracted service and counted by nobody — which is precisely the
#: failure this module's own docstring is about. The ledger counts the
#: platform, so it scans the platform.
_SERVICES = Path(__file__).resolve().parents[3]
ROUTER_DIRS = [
    Path(__file__).resolve().parents[1] / "api" / "routers",
    _SERVICES / "analytics-service" / "analytics_service" / "api" / "routers",
    _SERVICES / "hxgraph-service" / "hxgraph_service" / "api" / "routers",
    _SERVICES / "hxmigrate-service" / "hxmigrate_service" / "api" / "routers",
    # R5e step 7 moved six routers out of case-service into commerce-service.
    _SERVICES / "commerce-service" / "commerce_service" / "api" / "routers",
    # R5e step 8 moved the three portal routers into portal-service.
    _SERVICES / "portal-service" / "portal_service" / "api" / "routers",
    # R5e step 10 moved the six sign-in and identity routers into user-service.
    _SERVICES / "user-service" / "user_service" / "api" / "routers",
]

#: Kept for callers that only care about this service's own routers.
ROUTERS = ROUTER_DIRS[0]


def find_router(name: str) -> Path:
    """Locate a router file by name across every service. Raises if missing."""
    for d in ROUTER_DIRS:
        candidate = d / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"router {name!r} is in the authorization ledger but exists in no service: "
        f"{[str(d) for d in ROUTER_DIRS]}"
    )

#: Attribute reads on the authenticated user that are authorization inputs.
SIGNAL_ATTRS = frozenset({"is_admin", "is_designer", "is_superadmin", "roles"})
#: Method calls on the authenticated user that are authorization inputs.
SIGNAL_METHODS = frozenset({"has_role", "has_privilege"})


@dataclass(frozen=True)
class LegacySite:
    file: str
    function: str
    #: "GET /path" for a routed endpoint, "" for a helper.
    route: str
    #: Signals read directly here; empty when it inherits them from a helper.
    direct: tuple[str, ...]
    #: True when this function also carries an `hxguard.guard(...)` dependency,
    #: i.e. the gate is migrated but the body still decides on role names too.
    also_guarded: bool


def _direct_signals(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in SIGNAL_ATTRS:
            found.add(n.attr)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in SIGNAL_METHODS):
            found.add(n.func.attr)
    return found


def _called_names(node: ast.AST) -> set[str]:
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _route_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for d in fn.decorator_list:
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"):
            path = d.args[0].value if d.args and isinstance(d.args[0], ast.Constant) else ""
            return f"{d.func.attr.upper()} {path}".strip()
    return ""


def scan_file(path: Path) -> list[LegacySite]:
    tree = ast.parse(path.read_text())
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    direct = {name: _direct_signals(n) for name, n in fns.items()}
    calls = {name: _called_names(n) for name, n in fns.items()}

    tainted = {n for n, s in direct.items() if s}
    changed = True
    while changed:                       # transitive closure over local calls
        changed = False
        for name, called in calls.items():
            if name not in tainted and (called & tainted):
                tainted.add(name)
                changed = True

    out = []
    for name in sorted(tainted):
        fn = fns[name]
        src = ast.unparse(fn)
        out.append(LegacySite(
            file=path.name, function=name, route=_route_of(fn),
            direct=tuple(sorted(direct[name])),
            also_guarded="hxguard.guard(" in src or "hxguard.require(" in src,
        ))
    return out


def scan() -> list[LegacySite]:
    """Every router function that still decides on role names, sorted stably."""
    sites: list[LegacySite] = []
    for path in sorted(p for d in ROUTER_DIRS if d.is_dir() for p in d.glob("*.py")):
        sites.extend(scan_file(path))
    return sorted(sites, key=lambda s: (s.file, s.function))


def summary() -> dict[str, int]:
    sites = scan()
    endpoints = [s for s in sites if s.route]
    return {
        "sites": len(sites),
        "routers": len({s.file for s in sites}),
        "endpoints": len(endpoints),
        "helpers": len(sites) - len(endpoints),
        "endpoints_already_guarded": sum(1 for s in endpoints if s.also_guarded),
    }


#: The recorded population, 2026-08-14. Every entry is work Phase 4 must not
#: run past. `tests/phase_authz` compares this to a live scan, so migrating a
#: router means deleting its lines here in the same change — the ledger cannot
#: drift from the code the way `hxguard.md`'s follow-up list drifted from
#: `TODO.md`.
#:
#: Format: "file.py::function".
EXPECTED_SITES: frozenset[str] = frozenset()      # populated below


def _load_expected() -> frozenset[str]:
    path = Path(__file__).with_name("legacy_sites.txt")
    if not path.exists():
        return frozenset()
    return frozenset(
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )


EXPECTED_SITES = _load_expected()


__all__ = ["LegacySite", "scan", "scan_file", "summary", "EXPECTED_SITES",
           "SIGNAL_ATTRS", "SIGNAL_METHODS"]
