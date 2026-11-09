"""R5e step 10 — the sign-in split, wired end to end (deploy side).

The gateway, ingress, Helm, internal TLS, the units and the K8 scripts each have
to know user-service; any one of them missing it is a platform nobody can sign
in to — or `POST /api/v1/auth/switch-context` (case-service's) and case-service's
identity verification (`/api/v1/identity/cases/…`) sent to the wrong place.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
NGINX = ROOT / "deploy/docker-compose/gateway/nginx.conf.template"


def _nginx_route(path: str) -> str:
    """nginx's rule: an exact `location = <path>` wins; otherwise the LONGEST
    matching prefix (no regex location covers these paths)."""
    text = NGINX.read_text()
    best, upstream = "", ""
    # A block body may hold `${VAR}`, so `}` alone does not end it.
    for m in re.finditer(r"location\s+(=\s*)?(/[^\s{]*)\s*\{((?:[^{}]|\$\{[A-Z_]+\})*)\}", text):
        exact, prefix, body = bool(m.group(1)), m.group(2), m.group(3)
        up = re.search(r"proxy_pass\s+\$\{VELARIS_UPSTREAM_SCHEME\}://(\w+)", body)
        if not up:
            continue
        if exact:
            if path == prefix:
                return up.group(1)
            continue
        if path.startswith(prefix) and len(prefix) > len(best):
            best, upstream = prefix, up.group(1)
    return upstream


@pytest.mark.parametrize("path,upstream", [
    ("/api/v1/auth/real/login", "user_service"),
    ("/api/v1/auth/login", "user_service"),
    ("/api/v1/auth/me", "user_service"),
    ("/api/v1/auth/saml/acme/acs", "user_service"),
    ("/api/v1/scim/v2/Users", "user_service"),
    ("/api/v1/scim/tokens", "user_service"),
    ("/api/v1/identity/provisioning/group-mappings", "user_service"),
    ("/api/v1/user-directory", "user_service"),
    ("/api/v1/user-directory/abc", "user_service"),
    # case-service's — must NOT move:
    ("/api/v1/auth/switch-context", "case_service"),
    ("/api/v1/identity/cases/123/verify", "case_service"),
    ("/api/v1/identity/verifications", "case_service"),
    ("/api/v1/identity/webhooks/onfido", "case_service"),
    ("/api/v1/access-groups", "case_service"),
])
def test_gateway_routes_sign_in_and_nothing_else(path, upstream):
    assert _nginx_route(path) == upstream


def test_every_user_service_route_reaches_it_and_no_case_route_does():
    """Against the apps themselves, not a hand-written list."""
    from case_service.main import app as case_app
    from fastapi.routing import APIRoute
    from user_service.main import app as user_app

    case = getattr(case_app, "_case", case_app)          # the test gateway, if installed
    for r in user_app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api/"):
            p = re.sub(r"\{[^}]+\}", "x", r.path)
            assert _nginx_route(p) == "user_service", r.path
    for r in case.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api/"):
            p = re.sub(r"\{[^}]+\}", "x", r.path)
            assert _nginx_route(p) != "user_service", r.path


def test_gateway_upstream_uses_the_port_variable():
    assert "upstream user_service      { server 127.0.0.1:${VELARIS_USER_PORT}; }" in NGINX.read_text()


def test_internal_certificate_is_a_client_too():
    ca = (ROOT / "deploy/tls/make-internal-ca.sh").read_text()
    rot = (ROOT / "deploy/tls/rotate-internal-certs.sh").read_text()
    assert re.search(r"CALLERS=\([^)]*user-service", ca)
    assert re.search(r"LEAVES=\([^)]*user-service", rot)
    assert re.search(r'\|user-service\)[^\n]*usage="serverAuth, clientAuth"', rot)
    assert "velaris-user-service:8206" in rot


def test_unit_and_companion():
    unit = (ROOT / "deploy/systemd/velaris-user-service.service").read_text()
    assert "user_service.main:app --host 127.0.0.1 --port ${VELARIS_USER_PORT}" in unit
    assert "Environment=VELARIS_USER_PORT=8206" in unit
    exe = (ROOT / "deploy/systemd/velaris-exec.sh").read_text()
    assert 'user_service.main:app)      _svc="user-service"' in exe
    for f in ("install.sh", "refresh-after-update.sh"):
        assert "velaris-user-service" in (ROOT / "deploy/systemd" / f).read_text(), f


def test_scripts_know_it():
    assert 'pkill -f "uvicorn user_service"' in (ROOT / "stop-velaris.sh").read_text()
    assert '"uvicorn user_service"' in (ROOT / "uninstall-velaris.sh").read_text()
    # Probed when the release's gateway routes to it (R3 2026-11-01: a rollback
    # target without user-service must not fail the gate on its port).
    assert '_routed user_service      && _SVC_PROBES="$_SVC_PROBES ${VELARIS_USER_PORT:-8206}:user-service"' \
        in (ROOT / "update-velaris.sh").read_text()
    assert "_write_if_missing VELARIS_USER_PORT" in (ROOT / "setup-velaris.sh").read_text()
    backup = (ROOT / "scripts/backup/velaris_backup.py").read_text()
    assert '"VELARIS_USER_PORT"' in backup and '"velaris-user-service"' in backup


def test_the_service_names_itself_before_anything_reads_it():
    """The internal client picks its certificate, and the database layer its
    pool, from VELARIS_SERVICE_NAME; the package sets it on import."""
    init = (ROOT / "services/user-service/user_service/__init__.py").read_text()
    assert 'os.environ.setdefault("VELARIS_SERVICE_NAME", "user-service")' in init


def test_dockerfile_installs_what_it_imports():
    df = (ROOT / "services/user-service/Dockerfile").read_text()
    assert "COPY libs/velaris-sdk" in df          # BRAND_NAME (SAML metadata, mail text)
    assert '"user_service.main:app"' in df and "USER 10001:10001" in df


@pytest.mark.parametrize("db", ["Postgres", "MySQL", "MariaDB"])
def test_k8_scripts_build_scale_and_deploy_it(db):
    common = (ROOT / f"K8/{db}/_common.sh").read_text()
    setup = (ROOT / f"K8/{db}/setup-velaris-k8s.sh").read_text()
    update = (ROOT / f"K8/{db}/update-velaris-k8s.sh").read_text()
    assert re.search(r"WORKLOADS=\([^)]*user-service", common)
    assert 'image.userService.repository="$reg/user-service"' in common
    assert re.search(r"IMAGES=\([^)]*user-service", setup)
    assert "[user-service]=userService" in update


def _helm(*sets: str) -> str:
    args = ["helm", "template", "t", str(ROOT / "deploy/helm/velaris")]
    for s in sets:
        args += ["--set", s]
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout + r.stderr


@pytest.mark.skipif(subprocess.run(["which", "helm"], capture_output=True).returncode != 0,
                    reason="helm not installed")
class TestHelm:
    def test_deployment_and_service(self):
        out = _helm()
        assert "name: t-helix-user-service" in out
        assert "value: user-service" in out              # VELARIS_SERVICE_NAME

    def test_ingress_paths(self):
        out = _helm("ingress.enabled=true")
        for p in ("/api/v1/auth", "/api/v1/scim", "/api/v1/identity/provisioning",
                  "/api/v1/user-directory"):
            block = out[out.index(f"- path: {p}\n"):][:600]
            assert "name: t-helix-user-service" in block, p
        block = out[out.index("- path: /api/v1/auth/switch-context\n"):][:600]
        assert "pathType: Exact" in block and "name: t-helix-case-service" in block

    def test_networkpolicy_and_pdb(self):
        out = _helm("networkPolicy.enabled=true", "podDisruptionBudget.enabled=true")
        assert re.search(r"kind: NetworkPolicy\nmetadata:\n  name: t-helix-user-service", out)
        assert re.search(r"kind: PodDisruptionBudget\nmetadata:\n  name: t-helix-user-service", out)

    def test_own_pool_when_the_pooler_is_on(self):
        out = _helm("pooler.enabled=true")
        assert "${DB_NAME}_user = host=$DB_HOST" in out
        assert "@t-helix-pooler:6432/helix_user" in out

    def test_no_own_pool_when_the_pooler_is_off(self):
        out = _helm()
        assert "USER_DATABASE_POOLER_URL" not in out and "pooler-user-url" not in out
