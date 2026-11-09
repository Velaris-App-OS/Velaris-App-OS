"""R1 — no single empty or mistyped line in .env stops a service from starting.

Every setting × every bad value (2026-10-30: 106 settings stopped a boot on
`KEY=` or a typo), the two deliberate refusals, case enforcement failing closed,
and every render the scripts and containers produce: the gateway, the service
ports on both launch paths, the replication gateway's compose ports and the
pooler's sizes.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from velaris_shared.config import Settings, _env_name, env_int

ROOT = Path(__file__).resolve().parents[4]
BAD = ["", "   ", "abc", "9" * 40, "-1", "1.5.6"]
REFUSED = {"auth_secret", "database_url"}


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("VELARIS") or k in ("DATABASE_BACKEND",):
            monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _fields():
    return [(n, f) for n, f in Settings.model_fields.items()]


@pytest.mark.parametrize("bad", BAD)
def test_no_setting_stops_a_boot(clean_env, bad):
    stopped = []
    for name, field in _fields():
        env = _env_name(name, field)
        clean_env.setenv(env, bad)
        try:
            Settings(_env_file=None)
        except Exception:                                  # noqa: BLE001
            stopped.append(env)
        clean_env.delenv(env)
    expected = {_env_name(n, Settings.model_fields[n]) for n in REFUSED} if not bad.strip() else set()
    assert set(stopped) == expected


@pytest.mark.parametrize("bad", ["", "  ", "abc", "-1"])
def test_a_bad_number_or_switch_is_its_default(clean_env, bad):
    for name, field in _fields():
        if field.annotation not in (int, float, bool):
            continue
        env = _env_name(name, field)
        clean_env.setenv(env, bad)
        assert getattr(Settings(_env_file=None), name) == field.default, env
        clean_env.delenv(env)


def test_a_good_value_is_still_read(clean_env):
    clean_env.setenv("VELARIS_CASE_SERVICE_PORT", " 9300 ")
    clean_env.setenv("VELARIS_CASE_PII_MASK_AI_EGRESS", "Yes")
    clean_env.setenv("VELARIS_CASE_HXNEXUS_RERANK_LEXICAL_WEIGHT", "0.5")
    s = Settings(_env_file=None)
    assert (s.service_port, s.pii_mask_ai_egress, s.hxnexus_rerank_lexical_weight) == (9300, True, 0.5)


def test_a_bad_value_is_named_in_the_log(clean_env, caplog):
    clean_env.setenv("VELARIS_CASE_SERVICE_PORT", "82OO")
    Settings(_env_file=None)
    assert "VELARIS_CASE_SERVICE_PORT='82OO' is not a valid whole number" in caplog.text


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_an_empty_secret_or_database_refuses_naming_the_variable(clean_env, name):
    env = _env_name(name, Settings.model_fields[name])
    clean_env.setenv(env, "  ")
    with pytest.raises(Exception, match=env):
        Settings(_env_file=None)


@pytest.mark.parametrize("value,mode", [
    ("", "enforce"), ("  ", "enforce"), ("enfroce", "enforce"), ("Enforce ", "enforce"),
    ("shadow", "shadow"), (" OFF", "off"),
])
def test_case_enforcement_is_never_opened_by_a_bad_line(clean_env, value, mode):
    # Empty read as "shadow" — every case gate advisory — until 2026-10-30.
    clean_env.setenv("VELARIS_CASE_HXGUARD_CASE_ENFORCEMENT", value)
    assert Settings(_env_file=None).hxguard_case_enforcement == mode


def test_an_empty_mode_or_place_is_its_default(clean_env):
    clean_env.setenv("VELARIS_CASE_AUTH_ISSUER", "")
    clean_env.setenv("VELARIS_CASE_REDIS_URL", " ")
    s = Settings(_env_file=None)
    assert s.auth_issuer == "velaris" and s.redis_url == "redis://localhost:6379/0"


@pytest.mark.parametrize("raw,expected", [(None, 7), ("", 7), ("x9", 7), ("-3", 7), ("12", 12)])
def test_env_int_outside_settings(clean_env, raw, expected):
    if raw is not None:
        clean_env.setenv("VELARIS_TEST_R1_INT", raw)
    assert env_int("VELARIS_TEST_R1_INT", 7) == expected


# ── renders ──────────────────────────────────────────────────────────────────

def _sh(script: str, env: dict[str, str], shell: str = "bash") -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if not k.startswith("VELARIS") and k != "INVOCATION_ID"}
    e.update(env)
    return subprocess.run([shell, "-c", script], env=e, capture_output=True, text=True, timeout=30)


def test_the_gateway_replaces_a_mistyped_value_before_the_render():
    envsh = ROOT / "deploy/docker-compose/gateway/10-velaris-defaults.envsh"
    assert os.access(envsh, os.X_OK), "the nginx entrypoint sources only executable .envsh"
    r = _sh(f'. "{envsh}"; echo "$VELARIS_CASE_PORT $VELARIS_GW_RATE_API $VELARIS_GW_WS_CONN_MAX '
            f'$VELARIS_UPSTREAM_SCHEME $VELARIS_USER_PORT $VELARIS_GW_RATE_PORTAL"',
            {"VELARIS_CASE_PORT": "820l", "VELARIS_GW_RATE_API": "20/s",
             "VELARIS_GW_WS_CONN_MAX": "lots", "VELARIS_UPSTREAM_SCHEME": "htps",
             "VELARIS_USER_PORT": "9306", "VELARIS_GW_RATE_PORTAL": "2r/s"}, shell="sh")
    assert r.stdout.split() == ["8201", "20r/s", "256", "http", "9306", "2r/s"]
    assert "VELARIS_CASE_PORT='820l' is not valid" in r.stderr
    compose = (ROOT / "deploy/docker-compose/docker-compose.yml").read_text()
    assert "/docker-entrypoint.d/10-velaris-defaults.envsh:ro" in compose


def test_the_gateway_refuses_a_secret_it_cannot_carry():
    envsh = ROOT / "deploy/docker-compose/gateway/10-velaris-defaults.envsh"
    r = _sh(f'. "{envsh}"; echo reached', {"VELARIS_GATEWAY_IDENTITY_SECRET": 'ab"c'}, shell="sh")
    assert r.returncode == 1 and "reached" not in r.stdout
    assert "VELARIS_GATEWAY_IDENTITY_SECRET" in r.stderr
    # Only what breaks nginx is refused: a working install's secret keeps working.
    r = _sh(f'. "{envsh}"; echo reached', {"VELARIS_GATEWAY_IDENTITY_SECRET": "p@ss!w#rd%^&*();{}"}, shell="sh")
    assert r.returncode == 0 and "reached" in r.stdout
    for bad in ("a\\b", "a$b", "a\nb"):
        assert _sh(f'. "{envsh}"', {"VELARIS_GATEWAY_IDENTITY_SECRET": bad}, shell="sh").returncode == 1, bad
    r = _sh(f'. "{envsh}"; echo "[$VELARIS_GATEWAY_IDENTITY_SECRET]"', {}, shell="sh")
    assert r.stdout.strip() == "[]"     # unset renders empty, never a literal ${...}


@pytest.mark.parametrize("value,port", [("", "8201"), ("820l", "8201"), ("99999", "8201"), ("9301", "9301")])
def test_the_systemd_wrapper_fixes_a_bad_port(value, port):
    r = _sh(f'bash "{ROOT}/deploy/systemd/velaris-exec.sh" "{ROOT}" echo uvicorn case_service.main:app '
            f'--host 127.0.0.1 --port "{value}" --app-dir x', {})
    assert f"--port {port} --app-dir x" in r.stdout, r.stderr


def test_start_velaris_checks_every_port():
    start = (ROOT / "start-velaris.sh").read_text()
    assert '. "$VELARIS_DIR/deploy/ports.sh"' in start
    for var in ("VELARIS_ENGINE_PORT", "VELARIS_CASE_PORT", "VELARIS_ANALYTICS_PORT",
                "VELARIS_HXGRAPH_PORT", "VELARIS_HXMIGRATE_PORT", "VELARIS_STUDIO_PORT",
                "VELARIS_COMMERCE_PORT", "VELARIS_PORTAL_PORT", "VELARIS_WORKER_PORT",
                "VELARIS_USER_PORT"):
        assert f"{var}:" in start.split('deploy/ports.sh"', 1)[1].split("done", 1)[0], var


def test_compose_never_maps_the_raw_replication_port(tmp_path):
    compose = (ROOT / "deploy/docker-compose/docker-compose.yml").read_text()
    assert "${VELARIS_REPLICATION_PORT" not in compose
    assert "${VELARIS_REPLICATION_GW_PORT:-5442}" in compose
    (tmp_path / ".env").write_text("VELARIS_REPLICATION_PORT=54x2\nVELARIS_REPLICATION_TEMPORAL_PORT=6543\n")
    r = _sh(f'. "{ROOT}/deploy/docker-compose/compose-profiles.sh"; '
            'echo "$VELARIS_REPLICATION_GW_PORT $VELARIS_REPLICATION_GW_TEMPORAL_PORT"',
            {"VELARIS_DIR": str(tmp_path)})
    assert r.stdout.split() == ["5442", "6543"]
    assert "VELARIS_REPLICATION_PORT='54x2' is not a port" in r.stderr


def test_the_pooler_falls_back_on_a_bad_size():
    compose = (ROOT / "deploy/docker-compose/docker-compose.yml").read_text()
    block = compose.split("  velaris-pooler:", 1)[1].split("\n  # ", 1)[0]
    for var in ("POOL_SIZE", "MAX_CLIENT_CONN", "COMMERCE_POOL_SIZE", "PORTAL_POOL_SIZE", "USER_POOL_SIZE"):
        assert f'VELARIS_POOLER_{var}="$$(num ' in block, var
    assert block.index("$$(num ") < block.index('cat > "$$POOLER_CFG/pgbouncer.ini"')


# ── Kubernetes: the token signing secret ─────────────────────────────────────

def _helm(*args: str) -> str:
    import shutil
    if not shutil.which("helm"):
        pytest.skip("helm not installed")
    r = subprocess.run(["helm", "template", "t", str(ROOT / "deploy/helm/velaris"), *args],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_every_kubernetes_service_gets_a_generated_signing_secret():
    # The chart set none until 2026-10-30: every pod signed tokens with the
    # default string in the source, so anyone could mint an admin token.
    out = _helm("--set", "jobs.enabled=true")
    docs = [d for d in out.split("\n---") if "kind: Deployment" in d]
    services = [d for d in docs if "VELARIS_CASE_DATABASE_URL" in d]
    assert len(services) == 9
    for d in services:
        assert "name: VELARIS_CASE_AUTH_SECRET" in d and "name: t-helix-auth" in d
    secret = out.split("name: t-helix-auth", 1)[0].rsplit("\n---", 1)[1]
    assert "kind: Secret" in secret
    assert "helm.sh/resource-policy: keep" in out.split("name: t-helix-auth", 1)[1].split("\n---", 1)[0]
    tpl = (ROOT / "deploy/helm/velaris/templates/auth-secret.yaml").read_text()
    assert 'lookup "v1" "Secret"' in tpl          # an upgrade never mints a new one


def test_an_operators_own_signing_secret_is_used():
    out = _helm("--set", "authSecret.existingSecret=mine")
    assert "name: t-helix-auth" not in out
    assert out.count("name: mine") == 8


# R3 re-run 2026-11-01: every MySQL/MariaDB install's setup writes an EMPTY
# VELARIS_CASE_DATABASE_URL plus the DB_HOST/_PORT/_NAME/_USER components; the
# backend builds the URL from them. Refusing the empty URL stopped every such
# install from starting on upgrade.
@pytest.mark.parametrize("env", [
    {"VELARIS_CASE_DATABASE_BACKEND": "mysql", "VELARIS_CASE_DB_HOST": "db.internal"},
    {"VELARIS_CASE_DATABASE_BACKEND": "mariadb"},
    {"VELARIS_CASE_DB_HOST": "pg.internal"},
])
def test_empty_database_url_with_components_starts(clean_env, monkeypatch, env):
    monkeypatch.setenv("VELARIS_CASE_DATABASE_URL", "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert Settings(_env_file=None).database_url == ""


def test_empty_database_url_with_nothing_else_is_refused(clean_env, monkeypatch):
    monkeypatch.setenv("VELARIS_CASE_DATABASE_URL", "")
    with pytest.raises(Exception, match="VELARIS_CASE_DB_HOST is not set"):
        Settings(_env_file=None)
