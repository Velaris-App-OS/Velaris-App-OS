"""R5e step 8 — the portal split, wired end to end (deploy side).

The gateway, ingress, Helm, internal TLS and the K8 scripts each have to know
portal-service; any one of them missing it is a portal that answers 502 or a
case-service router (`/api/v1/portals`) sent to the wrong place.

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
    """nginx's prefix rule: the LONGEST matching `location <prefix>` wins."""
    text = NGINX.read_text()
    best, upstream = "", ""
    # A block body may hold `${VAR}`, so `}` alone does not end it.
    for m in re.finditer(r"location\s+(/[^\s{]*)\s*\{((?:[^{}]|\$\{[A-Z_]+\})*)\}", text):
        prefix, body = m.group(1), m.group(2)
        up = re.search(r"proxy_pass\s+\$\{VELARIS_UPSTREAM_SCHEME\}://(\w+)", body)
        if up and path.startswith(prefix) and len(prefix) > len(best):
            best, upstream = prefix, up.group(1)
    return upstream


@pytest.mark.parametrize("path,upstream", [
    ("/api/v1/portal/acme/submit", "portal_service"),
    ("/api/v1/portal/acme/account/cases", "portal_service"),
    ("/api/v1/portal-admin/tenants", "portal_service"),
    ("/api/v1/portals", "case_service"),            # a case-service router
    ("/api/v1/portals/123", "case_service"),
])
def test_gateway_routes_the_portal_and_nothing_else(path, upstream):
    assert _nginx_route(path) == upstream


def test_portal_keeps_its_rate_limit():
    text = NGINX.read_text()
    for loc in ("/api/v1/portal/", "/api/v1/portal-admin/"):
        block = text[text.index(f"location {loc} {{"):][:300]
        assert "zone=velaris_portal" in block, loc


def test_gateway_upstream_uses_the_port_variable():
    assert "upstream portal_service    { server 127.0.0.1:${VELARIS_PORTAL_PORT}; }" in NGINX.read_text()


def test_internal_certificate_is_a_client_too():
    ca = (ROOT / "deploy/tls/make-internal-ca.sh").read_text()
    rot = (ROOT / "deploy/tls/rotate-internal-certs.sh").read_text()
    assert re.search(r"CALLERS=\([^)]*portal-service", ca)
    assert re.search(r"LEAVES=\([^)]*portal-service", rot)
    assert re.search(r'commerce-service\|portal-service[|)][^\n]*usage="serverAuth, clientAuth"', rot)
    assert "velaris-portal-service:8205" in rot


def test_a_missing_certificate_is_issued_from_the_existing_ca():
    text = (ROOT / "deploy/tls/uvicorn-tls-args.sh").read_text()
    assert '[ -f "$dir/ca.crt" ] && [ -f "$dir/ca.key" ]' in text
    # Issued before the plaintext fallback, never after.
    assert text.index("has no certificate yet") < text.index("Starting $service WITHOUT TLS")


def test_companion_is_not_reported_as_unmanaged_by_rotation():
    rot = (ROOT / "deploy/tls/rotate-internal-certs.sh").read_text()
    assert 'systemctl is-active --quiet velaris-case-service 2>/dev/null; then\n    continue' in rot


@pytest.mark.parametrize("db", ["Postgres", "MySQL", "MariaDB"])
def test_k8_scripts_build_scale_and_deploy_it(db):
    common = (ROOT / f"K8/{db}/_common.sh").read_text()
    setup = (ROOT / f"K8/{db}/setup-velaris-k8s.sh").read_text()
    update = (ROOT / f"K8/{db}/update-velaris-k8s.sh").read_text()
    assert re.search(r"WORKLOADS=\([^)]*portal-service", common)
    assert 'image.portalService.repository="$reg/portal-service"' in common
    assert re.search(r"IMAGES=\([^)]*portal-service", setup)
    assert "[portal-service]=portalService" in update


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
        assert "name: t-helix-portal-service" in out
        assert "value: portal-service" in out            # VELARIS_SERVICE_NAME

    def test_ingress_paths(self):
        out = _helm("ingress.enabled=true")
        for p in ("/api/v1/portal", "/api/v1/portal-admin"):
            block = out[out.index(f"- path: {p}\n"):][:600]
            assert "name: t-helix-portal-service" in block, p

    def test_networkpolicy_and_pdb(self):
        out = _helm("networkPolicy.enabled=true", "podDisruptionBudget.enabled=true")
        assert re.search(r"kind: NetworkPolicy\nmetadata:\n  name: t-helix-portal-service", out)
        assert re.search(r"kind: PodDisruptionBudget\nmetadata:\n  name: t-helix-portal-service", out)

    def test_own_pool_when_the_pooler_is_on(self):
        out = _helm("pooler.enabled=true")
        assert "${DB_NAME}_portal = host=$DB_HOST" in out
        assert "@t-helix-pooler:6432/helix_portal" in out
