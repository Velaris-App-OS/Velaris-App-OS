"""Modular-boundary guard (roadmap #13 — keep service extraction cheap).

**13b is done: `velaris_shared` is a real package** at `services/_shared`, and
case-service installs it like any other dependency. That turns Rule 1 from a
convention into a fact — the shared layer physically cannot import a feature
module any more, because `case_service` is not among its dependencies. The test
below still checks it, since an import that fails at runtime is a worse way to
find out than a test.

* **Rule 1 — shared-layer purity.** `velaris_shared` (config / db / auth /
  hxguard) must NOT import `case_service`. Hard rule, no exceptions: the
  recorded `hxguard -> enterprise.security_events` debt was resolved at 13b by
  moving the security-event writer into `velaris_shared.db.security_events`,
  where its one real dependency (the model) already lived.

* **Rule 2 — extraction-target coupling ratchet.** Each service still to be
  extracted (13c analytics + process-mining, 13d graph, 13e hxmigrate) may only
  reach into case_service via its own package and a recorded baseline of known
  cross-deps. Any NEW cross-module import fails the test.

* **Rule 3 — inbound coupling ratchet.** Rule 2 only ever looked outward, so
  the calls *into* each target were never counted — and those are what decide
  whether an extraction is a move or a rewrite. They are recorded here too.

See docs/Future/Done/tier2-sequence-and-service-extraction.md.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi.routing import APIRoute

CS_ROOT = pathlib.Path(__file__).resolve().parents[2] / "case_service"
SHARED_ROOT = (
    pathlib.Path(__file__).resolve().parents[4] / "services" / "_shared" / "velaris_shared"
)
_PREFIX = "case_service."


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


# ─── Rule 1 — the shared package may not import the application ───

def test_shared_package_exists():
    """velaris_shared is a real installed package, not a directory in the app."""
    assert SHARED_ROOT.is_dir(), f"velaris_shared not found at {SHARED_ROOT}"
    for part in ("config.py", "db", "auth", "hxguard"):
        assert (SHARED_ROOT / part).exists(), f"velaris_shared is missing {part}"
    # And the app must not have kept a second copy: two importable paths to
    # db.models would be two SQLAlchemy Base registries.
    for part in ("config.py", "db", "auth", "hxguard"):
        assert not (CS_ROOT / part).exists(), (
            f"case_service/{part} came back — the shared layer must exist in exactly "
            f"one place, or the models get registered twice."
        )


def test_shared_package_does_not_import_case_service():
    """Hard rule: velaris_shared imports nothing from the application."""
    violations: list[str] = []
    for f in _iter_py(SHARED_ROOT):
        for imp in _case_service_imports(f):
            violations.append(f"  velaris_shared/{f.relative_to(SHARED_ROOT)}  ->  case_service.{imp}")
    assert not violations, (
        "velaris_shared must not import case_service — it is a dependency OF the "
        "application, not a part of it, and every extracted service installs it. "
        "Offenders:\n" + "\n".join(sorted(violations))
    )


# ─── Rule 2 — an extracted service may not import the application ──
# 13c-e are done: analytics/process-mining, hxgraph and the migration domain
# each run in their own process. The ratchet that used to record what a target
# still reached into now records the opposite and stronger property — that the
# extracted services depend on velaris_shared and on nothing else of ours.

SERVICES_ROOT = pathlib.Path(__file__).resolve().parents[4] / "services"

EXTRACTED = {
    "analytics-service": "analytics_service",
    "hxgraph-service": "hxgraph_service",
    "hxmigrate-service": "hxmigrate_service",
    "commerce-service": "commerce_service",   # R5e step 7
    "portal-service": "portal_service",       # R5e step 8
    "user-service": "user_service",           # R5e step 10
}


@pytest.mark.parametrize("directory,package", sorted(EXTRACTED.items()))
def test_extracted_service_does_not_import_case_service(directory, package):
    root = SERVICES_ROOT / directory / package
    assert root.is_dir(), f"{package} not found at {root}"
    violations: list[str] = []
    for f in _iter_py(root):
        for imp in _case_service_imports(f):
            violations.append(f"  {package}/{f.relative_to(root)}  ->  case_service.{imp}")
    assert not violations, (
        f"{package} imports case_service. An extracted service talks to case-service "
        f"through the gateway or shares code via velaris_shared — importing it directly "
        f"puts the monolith back in the process. Offenders:\n" + "\n".join(sorted(violations))
    )


@pytest.mark.parametrize("directory,package", sorted(EXTRACTED.items()))
def test_extracted_service_routes_are_not_also_mounted_here(directory, package):
    """The prefixes that moved must not come back in case-service.

    Two copies of the same route is worse than none: the gateway sends traffic
    to one of them, so a change applied to the other appears to do nothing.
    """
    import importlib

    main = importlib.import_module(f"{package}.main")
    moved = {r.path for r in main.app.routes if r.path.startswith("/api/v1/")}
    case_main = importlib.import_module("case_service.main")
    here = {r.path for r in case_main.app.routes}
    overlap = moved & here
    assert not overlap, (
        f"case-service still serves {sorted(overlap)[:5]}, which moved to {package}."
    )


# ─── Rule 3 — inbound coupling ratchet ────────────────────────────
# What in case-service still calls the code that moved. These are the calls
# that stayed in-process because the library lives in velaris_shared, and each
# one is a decision that was made deliberately at 13c-e:
#
#   * process_mining.event_logger  — a write on every case transition. A
#     network hop there buys isolation the case lifecycle does not want.
#   * process_mining.analyzer      — hxevolve calls five of its functions with
#     a live AsyncSession. REST would rewrite hxevolve, not move process-mining.
#   * analytics.query_engine       — the scheduler's ReportAction, same shape.
#   * hxgraph.sync / .query        — the scheduler, HxDBMigrate and the HxMCP
#     tool registry, same shape again.
#
# A NEW caller is not automatically wrong, but it is another thread tying the
# monolith to a service's library, so it has to be recorded here on purpose.

INBOUND: dict[str, set[str]] = {
    "analytics": {
        "scheduler/actions.py",          # export + structured query as a scheduled action
    },
    "process_mining": {
        "api/routers/cases.py",          # event_logger.log_event (module-level import)
        "core/case_lifecycle.py",        # event_logger.log_event on EVERY case transition
        "meet/service.py",               # event_logger.log_event
        "hxevolve/detector.py",          # analyzer (module-level import)
        "hxevolve/drift.py",             # analyzer
        "hxevolve/prover.py",            # analyzer (module-level import)
    },
    "hxgraph": {
        "scheduler/actions.py",          # sync_graph as a scheduled action
        "api/routers/hxdbmigrate.py",    # sync_graph after a schema import
        "hxmcp/registry.py",             # query_graph as an MCP tool
        # Oct item 4, reviewed: customer_graph reads only case-service's OWN
        # customer tables (never graph_nodes) and must run inside the 360's
        # per-case authz and Full-tier gate. Through the gateway it would need
        # a second copy of that gate on the far side.
        "engage/three_sixty.py",
    },
}


def _velaris_shared_imports(pyfile: pathlib.Path) -> set[str]:
    """Dotted velaris_shared.* module paths imported by *pyfile*."""
    tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
    prefix = "velaris_shared."
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.startswith(prefix):
                out.add(node.module[len(prefix):])
            elif node.level == 0 and node.module == "velaris_shared":
                out.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(prefix):
                    out.add(alias.name[len(prefix):])
    return out


@pytest.mark.parametrize("target", sorted(INBOUND))
def test_inbound_coupling_does_not_grow(target):
    """Nothing new may start calling an extracted service's library in-process."""
    callers: set[str] = set()
    for f in _iter_py(CS_ROOT):
        rel = str(f.relative_to(CS_ROOT))
        if any(_top(imp) == target for imp in _velaris_shared_imports(f)):
            callers.add(rel)
    new = callers - INBOUND[target]
    assert not new, (
        f"NEW in-process caller(s) of '{target}', which belongs to an extracted "
        f"service: {sorted(new)}.\n"
        f"Either call it through the gateway, or record it in INBOUND['{target}'] "
        f"as a deliberate, reviewed decision."
    )


# ─── Rule 4 — an extracted service must be the SAME door ──────────
# The quiet failure mode of extraction is not a missing route, it is a service
# that answers the same authenticated routes through a thinner stack. The
# gateway routes to it just as happily, and nothing looks wrong.
#
# Found by audit on 2026-08-23: all three extracted services were missing the
# superadmin boot gate, so on an install that had not finished setup
# case-service answered 503 on everything while they served their routes
# normally. A gate that is only true where it happens to be wired is not a gate.

#: Every middleware an extracted service must apply. `SuperadminGateMiddleware`
#: only reads `app.state.superadmin_missing`, so the lifespan that SETS it is
#: checked too — without it the gate is inert and reads as "configured".
REQUIRED_MIDDLEWARE = {
    "SuperadminGateMiddleware",
    "ObservabilityMiddleware",
    "AuditMiddleware",
    "RequestTrackingMiddleware",
    "CORSMiddleware",
    "BodyLimitMiddleware",
    "SecurityHeadersMiddleware",
}


@pytest.mark.parametrize("directory,package", sorted(EXTRACTED.items()))
def test_extracted_service_applies_the_platform_middleware(directory, package):
    import importlib

    app = importlib.import_module(f"{package}.main").app
    present = {m.cls.__name__ for m in app.user_middleware}
    missing = REQUIRED_MIDDLEWARE - present
    # The rate limiter is added only outside debug/pytest, so it is not required
    # here; everything else must be unconditional.
    assert not missing, (
        f"{package} is a weaker door than case-service — missing {sorted(missing)}. "
        f"Apply velaris_shared.app.apply_platform_middleware()."
    )


@pytest.mark.parametrize("directory,package", sorted(EXTRACTED.items()))
def test_extracted_service_arms_the_superadmin_gate(directory, package):
    """The gate middleware is useless without the lifespan that sets its flag.

    Checking that the middleware returns 503 when the flag is True proves only
    the middleware — it stays green with no lifespan at all, because the test
    itself set the flag. What has to be proven is that STARTUP sets it: delete
    the attribute, run the lifespan, and require that something put it back.
    `platform_lifespan` sets it either way (it fails open on a database error),
    so this passes with a database and without one, and fails only when the
    service forgot to wire the lifespan.
    """
    import importlib

    from fastapi.testclient import TestClient

    app = importlib.import_module(f"{package}.main").app

    if hasattr(app.state, "superadmin_missing"):
        delattr(app.state, "superadmin_missing")
    with TestClient(app):
        pass
    assert hasattr(app.state, "superadmin_missing"), (
        f"{package} did not evaluate the superadmin gate at startup — its "
        f"SuperadminGateMiddleware reads a flag nothing ever sets, so on an "
        f"install with no superadmin it would serve while case-service 503s. "
        f"Pass lifespan=velaris_shared.app.platform_lifespan(...) to FastAPI()."
    )

    # And the middleware must act on it. The app is the shared module-level
    # object other tests drive too: leaving the flag True made every later
    # request to this service 503 (found 2026-10-01 — three login-sweep tests
    # failed in full runs and passed alone). Always put it back.
    app.state.superadmin_missing = True
    try:
        client = TestClient(app, raise_server_exceptions=False)
        api = next((r.path for r in app.routes
                    if isinstance(r, APIRoute) and r.path.startswith("/api/v1/")), None)
        assert api, f"{package} serves no /api/v1 route"
        assert client.get(api).status_code == 503, (
            f"{package} served {api} on an install with no superadmin."
        )
        assert client.get("/health").status_code == 200, "health must stay probeable"
    finally:
        app.state.superadmin_missing = False


@pytest.mark.parametrize("directory,package", sorted(EXTRACTED.items()))
def test_extracted_service_exposes_its_metrics(directory, package):
    """ObservabilityMiddleware records either way; without /metrics nobody can
    scrape them and the service is a blind spot on the same dashboard."""
    import importlib

    app = importlib.import_module(f"{package}.main").app
    assert any(getattr(r, "path", None) == "/metrics" for r in app.routes), (
        f"{package} has no /metrics endpoint — call velaris_shared.app.mount_metrics()."
    )


# ─── Rule 5 — the gateway is the only caller (roadmap #18) ────────
# Loopback is a network boundary, not an identity: before this, any process on
# the host could open 127.0.0.1:8202 and reach an authenticated API without
# passing through the gateway. Every service must carry the check, and it must
# be inert only when no secret is configured.

ALL_SERVICE_PACKAGES = ["case_service", *sorted(EXTRACTED.values())]


@pytest.mark.parametrize("package", ALL_SERVICE_PACKAGES)
def test_every_service_enforces_workload_identity(package):
    import importlib

    app = importlib.import_module(f"{package}.main").app
    present = {m.cls.__name__ for m in app.user_middleware}
    assert "GatewayIdentityMiddleware" in present, (
        f"{package} does not check that a request came through the gateway, so it "
        f"can be called directly on its own port. Add GatewayIdentityMiddleware."
    )


@pytest.mark.parametrize("package", ALL_SERVICE_PACKAGES)
def test_workload_identity_refuses_and_exempts_the_right_paths(package):
    """Prove the behaviour, not the wiring.

    A middleware in the list is not a control; one that refuses is. And the
    exemptions matter as much as the refusal — systemd's ExecStartPre, Docker's
    health check and the metrics scrape all reach these directly, so a version
    that refused them would take the platform down on its first restart.
    """
    import importlib

    from fastapi.testclient import TestClient

    from velaris_shared.middleware.gateway_identity import (
        GatewayIdentityMiddleware, HEADER,
    )

    app = importlib.import_module(f"{package}.main").app
    mw = next(m for m in app.user_middleware if m.cls is GatewayIdentityMiddleware)
    secret = "test-secret-not-the-real-one"
    mw.kwargs["secret"] = secret            # type: ignore[index]
    app.middleware_stack = None             # force a rebuild with the new value
    try:
        client = TestClient(app, raise_server_exceptions=False)
        api = next(r.path for r in app.routes
                   if isinstance(r, APIRoute) and r.path.startswith("/api/v1/")
                   and "{" not in r.path)

        assert client.get(api).status_code == 403, (
            f"{package} answered {api} to a caller with no gateway credential"
        )
        assert client.get(api, headers={HEADER: "wrong"}).status_code == 403, (
            f"{package} accepted a WRONG gateway credential"
        )
        # With the credential it proceeds — 403 must not be the answer to
        # everything, or this test would pass on a broken service.
        assert client.get(api, headers={HEADER: secret}).status_code != 403

        for exempt in ("/health", "/ready", "/metrics"):
            if any(getattr(r, "path", None) == exempt for r in app.routes):
                assert client.get(exempt).status_code == 200, (
                    f"{package} refused {exempt} without a gateway credential — "
                    f"systemd, Docker and the metrics scrape all call it directly."
                )
    finally:
        mw.kwargs["secret"] = ""            # type: ignore[index]
        app.middleware_stack = None


def test_the_gateway_injects_the_identity_header_everywhere_it_proxies():
    """Every proxying scope, not just the server block.

    An nginx `location` that sets ANY `proxy_set_header` drops all the
    server-level ones — so a websocket location that re-declares its headers and
    forgets this one would refuse every realtime connection while ordinary
    requests kept working.
    """
    conf = (pathlib.Path(__file__).resolve().parents[4]
            / "deploy" / "docker-compose" / "gateway" / "nginx.conf.template").read_text()
    lines = conf.splitlines()

    # Every scope that declares proxy_set_header at all must declare this one.
    scopes: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("location ") or stripped.startswith("server {"):
            if current is not None:
                scopes.append(current)
            current = []
        if current is not None:
            current.append(stripped)
    if current is not None:
        scopes.append(current)

    offenders = []
    for scope in scopes:
        sets_headers = any(s.startswith("proxy_set_header") for s in scope)
        proxies = any(s.startswith("proxy_pass") for s in scope)
        has_identity = any("X-Velaris-Gateway" in s for s in scope)
        if sets_headers and proxies and not has_identity:
            offenders.append(scope[0])
    assert not offenders, (
        "gateway scopes that override headers but do not pass the workload "
        f"identity: {offenders}. Requests through them would be refused."
    )
