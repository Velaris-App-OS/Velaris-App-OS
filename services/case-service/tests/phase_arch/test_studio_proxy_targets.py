"""Studio's proxy must name 127.0.0.1, never `localhost`: the gateway and the
engine listen on 127.0.0.1 only, and where `localhost` also resolves to ::1
(Debian, RHEL, containers) Node tries ::1 first, is refused and does not retry —
every Studio API call answered 500 on a clean box (R6, 2026-10-30)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_no_proxy_target_names_localhost():
    src = (ROOT / "studio/vite.config.ts").read_text()
    assert not re.findall(r"target:\s*[\"'](?:https?|wss?)://localhost[:/\"']", src)
    assert re.findall(r"target:\s*\"http://127\.0\.0\.1:8200\"", src)


def test_studio_names_the_client_and_the_gateway_keeps_it_to_itself():
    """Every Studio user reached the gateway from loopback and shared one rate
    bucket (R6, 2026-10-30). Studio names the browser in X-Velaris-Client; the
    gateway keys its limits on it from a loopback peer only and strips it, so the
    services see nothing new (not X-Forwarded-For: their own per-address limit
    would start applying where it never has)."""
    vite = (ROOT / "studio/vite.config.ts").read_text()
    assert 'proxyReq.setHeader("X-Velaris-Client", fwd || peer)' in vite
    # a forwarded address is taken only from a loopback peer (a TLS front on the box)
    assert 'LOOPBACK.has(peer) ? String(req.headers["x-forwarded-for"]' in vite
    assert "Object.assign(route, { configure: nameTheClient })" in vite and "xfwd" not in vite
    nginx = (ROOT / "deploy/docker-compose/gateway/nginx.conf.template").read_text()
    assert "real_ip" not in nginx.replace("`set_real_ip_from`", "")
    assert re.search(r"map \$remote_addr \$velaris_loopback_peer \{ 127\.0\.0\.1 1; default 0; \}", nginx)
    zones = re.findall(r"^\s*limit_(?:req|conn)_zone (\S+) zone=(\w+)", nginx, re.M)
    assert zones and all(k == "$velaris_client" for k, _ in zones), zones
    gateways = len(re.findall(r'proxy_set_header X-Velaris-Gateway "', nginx))
    assert len(re.findall(r'proxy_set_header X-Velaris-Client "";', nginx)) == gateways


def test_the_services_get_the_client_overwritten_never_a_clients_own_entries():
    """X-Forwarded-For was APPENDED, so a client's own entries reached uvicorn and
    picked the services' per-address limit key — and every Studio user was
    127.0.0.1, one Redis bucket of 120/min for the whole install (R6, 2026-10-30).
    The gateway now overwrites it with the one address it believes."""
    nginx = (ROOT / "deploy/docker-compose/gateway/nginx.conf.template").read_text()
    assert "$proxy_add_x_forwarded_for" not in nginx
    xff = re.findall(r"proxy_set_header X-Forwarded-For\s+(\S+);", nginx)
    real = re.findall(r"proxy_set_header X-Real-IP\s+(\S+);", nginx)
    assert xff and set(xff) == {"$velaris_client"} and set(real) == {"$velaris_client"}
    for f in ("services/_shared/velaris_shared/app.py", "services/case-service/case_service/main.py"):
        src = (ROOT / f).read_text()
        assert src.count("requests_per_minute=1200, burst=40,") == 2 and "requests_per_minute=120," not in src, f
