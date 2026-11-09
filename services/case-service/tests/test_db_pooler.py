"""The connection pooler (Nov_TODO R5d 6a) — PgBouncer for PostgreSQL.

What would be silently wrong: an install with the setting unset or empty
connecting anywhere but directly, exactly as before; the pooler container
starting on an install that never asked for it (or on MySQL, or in front of a
database on another server); and, the other way, a set pooler URL that is
accepted but not used.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from velaris_shared.db import backends
from velaris_shared.db.backends import resolve_runtime_url

REPO = Path(__file__).resolve().parents[3]
DIRECT = "postgresql+asyncpg://velaris:pw@localhost:5432/velaris"
POOLED = "postgresql+asyncpg://velaris:pw@localhost:6432/velaris"


def _s(**kv):
    base = {"database_backend": "postgresql", "database_url": DIRECT, "database_pooler_url": ""}
    base.update(kv)
    return SimpleNamespace(**base)


# ── the application's URL ─────────────────────────────────────────────────────

class TestRuntimeUrl:
    @pytest.mark.parametrize("value", ["", "   "])
    def test_unset_or_empty_is_direct(self, value):
        assert resolve_runtime_url(_s(database_pooler_url=value)) == DIRECT

    def test_missing_attribute_is_direct(self):
        s = SimpleNamespace(database_backend="postgresql", database_url=DIRECT)
        assert resolve_runtime_url(s) == DIRECT

    def test_set_goes_through_the_pooler(self):
        assert resolve_runtime_url(_s(database_pooler_url=POOLED)) == POOLED

    def test_url_without_driver_gets_asyncpg(self):
        got = resolve_runtime_url(_s(database_pooler_url="postgresql://velaris:pw@localhost:6432/velaris"))
        assert got == POOLED

    def test_non_postgres_scheme_is_refused_by_name(self):
        with pytest.raises(SystemExit, match="VELARIS_CASE_DATABASE_POOLER_URL"):
            resolve_runtime_url(_s(database_pooler_url="mysql+aiomysql://u:p@h:6033/d"))

    @pytest.mark.parametrize("backend", ["mysql", "mariadb"])
    def test_ignored_on_mysql_family_never_a_failed_boot(self, backend, monkeypatch, caplog):
        monkeypatch.setattr(backends, "_pooler_warned", False)
        direct = "mysql+aiomysql://velaris:pw@localhost:3306/velaris"
        s = _s(database_backend=backend, database_url=direct, database_pooler_url=POOLED)
        assert resolve_runtime_url(s) == direct
        assert "PostgreSQL only" in caplog.text

    def test_direct_url_still_pinned_to_backend(self):
        # The pooler does not loosen the existing scheme pin on the direct URL.
        with pytest.raises(SystemExit):
            resolve_runtime_url(_s(database_backend="mysql", database_pooler_url=POOLED))


# ── commerce-service's own pool (R5e step 7) ──────────────────────────────────

COMMERCE = "postgresql+asyncpg://velaris:pw@localhost:6432/velaris_commerce"


class TestCommercePool:
    @pytest.fixture
    def as_commerce(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "commerce-service")

    def test_commerce_uses_its_own_pool(self, as_commerce):
        s = _s(database_pooler_url=POOLED, commerce_database_pooler_url=COMMERCE)
        assert resolve_runtime_url(s) == COMMERCE

    @pytest.mark.parametrize("value", ["", "   "])
    def test_commerce_unset_or_empty_shares_the_pool(self, as_commerce, value):
        s = _s(database_pooler_url=POOLED, commerce_database_pooler_url=value)
        assert resolve_runtime_url(s) == POOLED

    def test_commerce_missing_attribute_shares_the_pool(self, as_commerce):
        assert resolve_runtime_url(_s(database_pooler_url=POOLED)) == POOLED

    def test_never_without_the_shared_pooler(self, as_commerce):
        # Empty shared setting = no pooler container runs; commerce goes direct.
        s = _s(commerce_database_pooler_url=COMMERCE)
        assert resolve_runtime_url(s) == DIRECT

    @pytest.mark.parametrize("name", ["case-service", "analytics-service", ""])
    def test_other_services_ignore_it(self, monkeypatch, name):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", name)
        s = _s(database_pooler_url=POOLED, commerce_database_pooler_url=COMMERCE)
        assert resolve_runtime_url(s) == POOLED

    def test_unset_service_name_ignores_it(self, monkeypatch):
        monkeypatch.delenv("VELARIS_SERVICE_NAME", raising=False)
        s = _s(database_pooler_url=POOLED, commerce_database_pooler_url=COMMERCE)
        assert resolve_runtime_url(s) == POOLED

    def test_bad_scheme_is_refused_by_its_own_name(self, as_commerce):
        s = _s(database_pooler_url=POOLED, commerce_database_pooler_url="mysql+aiomysql://u:p@h:6033/d")
        with pytest.raises(SystemExit, match="VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL"):
            resolve_runtime_url(s)

    def test_ignored_on_mysql_family(self, as_commerce, monkeypatch):
        monkeypatch.setattr(backends, "_pooler_warned", False)
        direct = "mysql+aiomysql://velaris:pw@localhost:3306/velaris"
        s = _s(database_backend="mysql", database_url=direct,
               database_pooler_url=POOLED, commerce_database_pooler_url=COMMERCE)
        assert resolve_runtime_url(s) == direct

    def test_settings_reads_the_env_name(self, monkeypatch):
        from velaris_shared.config import Settings
        monkeypatch.setenv("VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL", COMMERCE)
        assert Settings().commerce_database_pooler_url == COMMERCE

    def test_pooler_defines_the_commerce_database(self):
        import yaml
        svc = yaml.safe_load((REPO / "deploy/docker-compose/docker-compose.yml").read_text())["services"]["velaris-pooler"]
        script = svc["command"][0]
        assert "velaris_commerce = host=velaris-db port=5432 dbname=velaris pool_size=" in script
        # Unset sizing still renders a number (compose and the script both default it).
        assert svc["environment"]["VELARIS_POOLER_COMMERCE_POOL_SIZE"] == "${VELARIS_CASE_POOLER_COMMERCE_POOL_SIZE:-10}"
        assert "$${VELARIS_POOLER_COMMERCE_POOL_SIZE:-10}" in (REPO / "deploy/docker-compose/docker-compose.yml").read_text()

    def test_setup_writes_it_only_beside_the_shared_pooler(self):
        text = (REPO / "setup-velaris.sh").read_text()
        shared = text.index('echo "VELARIS_CASE_DATABASE_POOLER_URL=')
        own = text.index('echo "VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL=')
        assert shared < own
        assert "@localhost:6432/velaris_commerce" in text
        # The update never writes it.
        assert "COMMERCE_DATABASE_POOLER_URL" not in (REPO / "update-velaris.sh").read_text()


# ── portal-service's own pool (R5e step 8) — same rules as commerce's ────────

PORTAL = "postgresql+asyncpg://velaris:pw@localhost:6432/velaris_portal"


class TestPortalPool:
    def test_portal_uses_its_own_pool(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "portal-service")
        s = _s(database_pooler_url=POOLED, portal_database_pooler_url=PORTAL,
               commerce_database_pooler_url=COMMERCE)
        assert resolve_runtime_url(s) == PORTAL

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_unset_shares_the_pool(self, monkeypatch, value):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "portal-service")
        kv = {} if value is None else {"portal_database_pooler_url": value}
        assert resolve_runtime_url(_s(database_pooler_url=POOLED, **kv)) == POOLED

    def test_never_without_the_shared_pooler(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "portal-service")
        assert resolve_runtime_url(_s(portal_database_pooler_url=PORTAL)) == DIRECT

    def test_commerce_never_takes_portals_pool(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "commerce-service")
        s = _s(database_pooler_url=POOLED, portal_database_pooler_url=PORTAL)
        assert resolve_runtime_url(s) == POOLED

    def test_bad_scheme_is_refused_by_its_own_name(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "portal-service")
        s = _s(database_pooler_url=POOLED, portal_database_pooler_url="mysql://u:p@h/d")
        with pytest.raises(SystemExit, match="VELARIS_CASE_PORTAL_DATABASE_POOLER_URL"):
            resolve_runtime_url(s)

    def test_pooler_defines_the_portal_database(self):
        text = (REPO / "deploy/docker-compose/docker-compose.yml").read_text()
        assert "velaris_portal = host=velaris-db port=5432 dbname=velaris pool_size=$${VELARIS_POOLER_PORTAL_POOL_SIZE:-10}" in text
        assert "VELARIS_POOLER_PORTAL_POOL_SIZE: ${VELARIS_CASE_POOLER_PORTAL_POOL_SIZE:-10}" in text

    def test_setup_writes_it_only_beside_the_shared_pooler(self):
        text = (REPO / "setup-velaris.sh").read_text()
        assert text.index('echo "VELARIS_CASE_DATABASE_POOLER_URL=') < \
               text.index('echo "VELARIS_CASE_PORTAL_DATABASE_POOLER_URL=')
        assert "PORTAL_DATABASE_POOLER_URL" not in (REPO / "update-velaris.sh").read_text()

    def test_twin_check_knows_the_portal_address(self):
        from case_service.hxdeploy import twin
        ok, detail = twin.isolation(SimpleNamespace(
            database_url=DIRECT, database_pooler_url=POOLED, portal_database_pooler_url=PORTAL,
            twin_database_url="postgresql://x:y@localhost:6432/velaris_portal",
            twin_task_queue="", temporal_task_queue="q"))
        assert ok is False and detail["database"]["same"] is True


# ── user-service's own pool (R5e step 10) — same rules again ─────────────────

USER = "postgresql+asyncpg://velaris:pw@localhost:6432/velaris_user"


class TestUserPool:
    def test_user_uses_its_own_pool(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "user-service")
        s = _s(database_pooler_url=POOLED, user_database_pooler_url=USER,
               portal_database_pooler_url=PORTAL, commerce_database_pooler_url=COMMERCE)
        assert resolve_runtime_url(s) == USER

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_unset_shares_the_pool(self, monkeypatch, value):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "user-service")
        kv = {} if value is None else {"user_database_pooler_url": value}
        assert resolve_runtime_url(_s(database_pooler_url=POOLED, **kv)) == POOLED

    def test_never_without_the_shared_pooler(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "user-service")
        assert resolve_runtime_url(_s(user_database_pooler_url=USER)) == DIRECT

    @pytest.mark.parametrize("svc", ["case-service", "commerce-service", "portal-service"])
    def test_no_other_service_takes_it(self, monkeypatch, svc):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", svc)
        assert resolve_runtime_url(_s(database_pooler_url=POOLED, user_database_pooler_url=USER)) == POOLED

    def test_bad_scheme_is_refused_by_its_own_name(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "user-service")
        s = _s(database_pooler_url=POOLED, user_database_pooler_url="mysql://u:p@h/d")
        with pytest.raises(SystemExit, match="VELARIS_CASE_USER_DATABASE_POOLER_URL"):
            resolve_runtime_url(s)

    def test_pooler_defines_the_user_database(self):
        text = (REPO / "deploy/docker-compose/docker-compose.yml").read_text()
        assert "velaris_user = host=velaris-db port=5432 dbname=velaris pool_size=$${VELARIS_POOLER_USER_POOL_SIZE:-10}" in text
        assert "VELARIS_POOLER_USER_POOL_SIZE: ${VELARIS_CASE_POOLER_USER_POOL_SIZE:-10}" in text

    def test_setup_writes_it_only_beside_the_shared_pooler(self):
        text = (REPO / "setup-velaris.sh").read_text()
        assert text.index('echo "VELARIS_CASE_DATABASE_POOLER_URL=') < \
               text.index('echo "VELARIS_CASE_USER_DATABASE_POOLER_URL=')
        assert "USER_DATABASE_POOLER_URL" not in (REPO / "update-velaris.sh").read_text()

    def test_twin_check_knows_the_user_address(self):
        from case_service.hxdeploy import twin
        ok, detail = twin.isolation(SimpleNamespace(
            database_url=DIRECT, database_pooler_url=POOLED, user_database_pooler_url=USER,
            twin_database_url="postgresql://x:y@localhost:6432/velaris_user",
            twin_task_queue="", temporal_task_queue="q"))
        assert ok is False and detail["database"]["same"] is True


def test_settings_reads_the_env_name(monkeypatch):
    from velaris_shared.config import get_settings
    monkeypatch.setenv("VELARIS_CASE_DATABASE_POOLER_URL", POOLED)
    get_settings.cache_clear()
    try:
        assert get_settings().database_pooler_url == POOLED
    finally:
        get_settings.cache_clear()


def test_session_engine_uses_runtime_url():
    # _make_engine must go through resolve_runtime_url, or the setting is
    # accepted and never used.
    src = (REPO / "services/_shared/velaris_shared/db/session.py").read_text()
    assert "resolve_runtime_url(settings)" in src
    assert "resolve_async_url(settings)" not in src


# ── the engine (helix_engine reads the environment itself) ───────────────────

class TestEngineUrl:
    @pytest.fixture
    def env(self, monkeypatch):
        for k in ("DATABASE_URL", "VELARIS_CASE_DATABASE_URL", "VELARIS_CASE_DATABASE_POOLER_URL"):
            monkeypatch.delenv(k, raising=False)
        from helix_engine.db.session import _database_url_from_env
        def _run(**kv):
            for k, v in kv.items():
                monkeypatch.setenv(k, v)
            return _database_url_from_env()
        return _run

    def test_unset_is_direct(self, env):
        assert env(VELARIS_CASE_DATABASE_URL=DIRECT) == DIRECT

    def test_empty_is_direct(self, env):
        assert env(VELARIS_CASE_DATABASE_URL=DIRECT, VELARIS_CASE_DATABASE_POOLER_URL="") == DIRECT

    def test_set_goes_through_the_pooler(self, env):
        assert env(VELARIS_CASE_DATABASE_URL=DIRECT, VELARIS_CASE_DATABASE_POOLER_URL=POOLED) == POOLED

    def test_database_url_still_wins(self, env):
        assert env(DATABASE_URL="postgresql+asyncpg://x@h/d",
                   VELARIS_CASE_DATABASE_POOLER_URL=POOLED) == "postgresql+asyncpg://x@h/d"

    def test_ignored_on_mysql(self, env):
        my = "mysql+aiomysql://velaris:pw@localhost:3306/velaris"
        assert env(VELARIS_CASE_DATABASE_URL=my, VELARIS_CASE_DATABASE_POOLER_URL=POOLED) == my


# ── which containers run (compose-profiles.sh) ───────────────────────────────

def _profiles(tmp_path: Path, env_lines: str, yaml_db: str = "postgresql", **extra) -> str:
    (tmp_path / ".env").write_text(env_lines)
    (tmp_path / "velaris.yaml").write_text(f"platform:\n  database: {yaml_db}\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "VELARIS_DIR": str(tmp_path),
           "VELARIS_BUNDLED_MINIO": "0", **extra}
    r = subprocess.run(
        ["bash", "-c", f'. "{REPO}/deploy/docker-compose/compose-profiles.sh"; '
                       'echo "$VELARIS_POOLER_ACTIVE|${COMPOSE_PROFILES:-}"'],
        env=env, capture_output=True, text=True, check=True)
    return r.stdout.strip()


class TestComposeProfile:
    def test_unset_runs_no_pooler(self, tmp_path):
        assert _profiles(tmp_path, "") == "false|"

    def test_empty_runs_no_pooler(self, tmp_path):
        assert _profiles(tmp_path, "VELARIS_CASE_DATABASE_POOLER_URL=\n") == "false|"

    def test_set_runs_the_pooler(self, tmp_path):
        assert _profiles(tmp_path, f"VELARIS_CASE_DATABASE_POOLER_URL={POOLED}\n") == "true|pooler"

    def test_environment_counts_too(self, tmp_path):
        assert _profiles(tmp_path, "", VELARIS_CASE_DATABASE_POOLER_URL=POOLED) == "true|pooler"

    @pytest.mark.parametrize("db", ["mysql", "mariadb"])
    def test_never_on_mysql_family(self, tmp_path, db):
        assert _profiles(tmp_path, f"VELARIS_CASE_DATABASE_POOLER_URL={POOLED}\n", yaml_db=db) == "false|"

    def test_never_in_front_of_an_external_database(self, tmp_path):
        lines = f"VELARIS_CASE_DATABASE_POOLER_URL={POOLED}\nVELARIS_EXTERNAL_SERVICES=database\n"
        assert _profiles(tmp_path, lines) == "false|"

    def test_operator_profiles_are_kept(self, tmp_path):
        lines = f"COMPOSE_PROFILES=observability\nVELARIS_CASE_DATABASE_POOLER_URL={POOLED}\n"
        assert _profiles(tmp_path, lines) == "true|observability,pooler"


def test_external_database_retires_the_bundled_pooler():
    r = subprocess.run(
        ["bash", "-c", f'. "{REPO}/deploy/external-services.sh"; _vx_services_of database'],
        capture_output=True, text=True, check=True)
    assert r.stdout.split() == ["velaris-db", "velaris-pooler"]


def test_compose_service_is_opt_in_pinned_and_loopback():
    import yaml
    svc = yaml.safe_load((REPO / "deploy/docker-compose/docker-compose.yml").read_text())["services"]["velaris-pooler"]
    assert svc["profiles"] == ["pooler"]
    image, tag = svc["image"].rsplit(":", 1)
    assert image == "ghcr.io/cloudnative-pg/pgbouncer"
    assert tuple(int(x) for x in tag.split(".")[:2]) >= (1, 21)      # asyncpg prepared statements
    assert svc["ports"] == ["127.0.0.1:6432:6432"]
    script = svc["command"][0]
    assert "pool_mode = transaction" in script
    assert "max_prepared_statements" in script
    # Unset password: compose must still interpolate for every install (`:-`, not `:?`).
    assert svc["environment"]["VELARIS_DB_PASSWORD"] == "${VELARIS_DB_PASSWORD:-}"


# ── systemd's wait for dependencies ───────────────────────────────────────────

def _wait_deps(tmp_path: Path, env_lines: str) -> subprocess.CompletedProcess:
    (tmp_path / ".env").write_text(env_lines)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "VELARIS_WAIT_DEPS_TIMEOUT": "2", "VELARIS_TEMPORAL_WAIT": "0"}
    return subprocess.run(["bash", str(REPO / "deploy/systemd/velaris-wait-deps.sh"), str(tmp_path)],
                          env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def open_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    yield s.getsockname()[1]
    s.close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestWaitDeps:
    def test_unset_does_not_wait_for_a_pooler(self, tmp_path, open_port):
        r = _wait_deps(tmp_path, f"VELARIS_CASE_DATABASE_URL=postgresql+asyncpg://v:p@127.0.0.1:{open_port}/v\n")
        assert "connection pooler" not in r.stdout + r.stderr

    def test_set_and_down_fails_by_name(self, tmp_path, open_port):
        down = _closed_port()
        r = _wait_deps(tmp_path,
                       f"VELARIS_CASE_DATABASE_URL=postgresql+asyncpg://v:p@127.0.0.1:{open_port}/v\n"
                       f"VELARIS_CASE_DATABASE_POOLER_URL=postgresql+asyncpg://v:p@127.0.0.1:{down}/v\n")
        assert r.returncode == 1
        assert f"connection pooler 127.0.0.1:{down} still refusing" in r.stderr

    def test_set_and_up_passes(self, tmp_path, open_port):
        r = _wait_deps(tmp_path,
                       f"VELARIS_CASE_DATABASE_URL=postgresql+asyncpg://v:p@127.0.0.1:{open_port}/v\n"
                       f"VELARIS_CASE_DATABASE_POOLER_URL=postgresql+asyncpg://v:p@127.0.0.1:{open_port}/v\n")
        assert f"connection pooler 127.0.0.1:{open_port} is accepting" in r.stdout

    def test_ignored_on_mysql(self, tmp_path, open_port):
        r = _wait_deps(tmp_path,
                       f"VELARIS_CASE_DATABASE_URL=mysql+aiomysql://v:p@127.0.0.1:{open_port}/v\n"
                       f"VELARIS_CASE_DATABASE_POOLER_URL=postgresql+asyncpg://v:p@127.0.0.1:{_closed_port()}/v\n")
        assert "connection pooler" not in r.stdout + r.stderr


# ── Helm ──────────────────────────────────────────────────────────────────────

def _helm(*sets: str) -> str:
    args = ["helm", "template", "t", str(REPO / "deploy/helm/velaris")]
    for s in sets:
        args += ["--set", s]
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout + r.stderr


@pytest.mark.skipif(subprocess.run(["which", "helm"], capture_output=True).returncode != 0,
                    reason="helm not installed")
class TestHelm:
    def test_off_by_default_renders_nothing(self):
        out = _helm()
        assert "pooler-url" not in out and "kind: Deployment\nmetadata:\n  name: t-helix-pooler" not in out
        assert "name: VELARIS_CASE_DATABASE_POOLER_URL" not in out

    def test_on_points_every_workload_at_it(self):
        out = _helm("pooler.enabled=true")
        assert out.count("name: VELARIS_CASE_DATABASE_POOLER_URL") == 8   # + commerce, portal, user (steps 7, 8, 10)
        assert "name: t-helix-pooler" in out
        assert "key: pooler-url" in out          # engine's DATABASE_URL too

    def test_off_renders_no_commerce_pool(self):
        out = _helm()
        assert "COMMERCE_DATABASE_POOLER_URL" not in out and "pooler-commerce-url" not in out

    def test_on_gives_commerce_its_own_pool(self):
        out = _helm("pooler.enabled=true")
        assert out.count("name: VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL") == 1
        assert "@t-helix-pooler:6432/helix_commerce" in out
        assert "${DB_NAME}_commerce = host=$DB_HOST port=5432 dbname=$DB_NAME pool_size=$COMMERCE_POOL_SIZE" in out

    def test_existing_secret_without_the_key_is_optional(self):
        out = _helm("pooler.enabled=true", "databaseSecret.existingSecret=mine")
        i = out.index("name: VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL")
        assert "optional: true" in out[i:i + 300]

    def test_on_without_bundled_postgres_is_refused(self):
        out = _helm("pooler.enabled=true", "postgresql.enabled=false")
        assert "pooler.enabled needs postgresql.enabled" in out


# ── the pooler over its UNIX socket (VELARIS_CASE_DATABASE_POOLER_SOCKET) ─────
#
# A loopback port is free while the pooler restarts; a local user who bound it
# then could pose as PostgreSQL and be sent the password in clear (asyncpg
# cannot refuse that). Over the socket in a 0700 folder only the install owner
# can reach — or impersonate — it. Unset = TCP, byte for byte as before.

class TestSocket:
    def test_unset_or_false_keeps_tcp_exactly(self):
        assert resolve_runtime_url(_s(database_pooler_url=POOLED)) == POOLED
        assert resolve_runtime_url(_s(database_pooler_url=POOLED, database_pooler_socket=False)) == POOLED

    def test_on_points_the_same_url_at_the_socket(self):
        from urllib.parse import parse_qs
        url = resolve_runtime_url(_s(database_pooler_url=POOLED + "?sslmode=disable&prepared_statement_cache_size=0",
                                     database_pooler_socket=True))
        assert url.startswith("postgresql+asyncpg://velaris:pw@/velaris?")
        q = parse_qs(url.split("?", 1)[1])
        assert q["host"] == [backends.pooler_socket_dir()] and q["port"] == ["6432"]
        assert q["prepared_statement_cache_size"] == ["0"] and "sslmode" not in q
        assert "localhost" not in url and "6432/" not in url

    def test_own_pools_keep_their_database_name(self, monkeypatch):
        monkeypatch.setenv("VELARIS_SERVICE_NAME", "commerce-service")
        url = resolve_runtime_url(_s(database_pooler_url=POOLED, database_pooler_socket=True,
                                     commerce_database_pooler_url=POOLED.replace("/velaris", "/velaris_commerce")))
        assert "@/velaris_commerce?" in url

    def test_never_on_without_a_pooler_or_on_mysql(self):
        assert resolve_runtime_url(_s(database_pooler_socket=True)) == DIRECT
        my = "mysql+aiomysql://u:p@localhost:3306/velaris"
        assert resolve_runtime_url(_s(database_backend="mysql", database_url=my,
                                      database_pooler_url=POOLED, database_pooler_socket=True)) == my

    def test_a_missing_folder_stops_with_its_name_never_falls_back_to_tcp(self, tmp_path):
        with pytest.raises(SystemExit, match="does not exist"):
            backends.pooler_socket_url(POOLED, str(tmp_path / "gone"))

    def test_the_folder_ships_with_the_release(self):
        d = REPO / "deploy/docker-compose/pooler-socket"
        assert d.is_dir() and (d / "README.md").is_file()
        assert Path(backends.pooler_socket_dir()) == d

    def test_the_engine_follows_the_same_setting(self, monkeypatch):
        from helix_engine.db.session import _database_url_from_env
        monkeypatch.setenv("VELARIS_CASE_DATABASE_URL", DIRECT)
        monkeypatch.setenv("VELARIS_CASE_DATABASE_POOLER_URL", POOLED)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("VELARIS_ENGINE_DATABASE_URL", raising=False)
        monkeypatch.delenv("VELARIS_CASE_DATABASE_POOLER_SOCKET", raising=False)
        assert _database_url_from_env() == POOLED
        monkeypatch.setenv("VELARIS_CASE_DATABASE_POOLER_SOCKET", "true")
        assert f"host={backends.pooler_socket_dir().replace('/', '%2F')}" in _database_url_from_env()

    def test_the_container_drops_root_and_uses_the_socket_only_when_on(self):
        import yaml
        svc = yaml.safe_load((REPO / "deploy/docker-compose/docker-compose.yml").read_text())["services"]["velaris-pooler"]
        script = svc["command"][0]
        assert svc["user"] == "0" and svc["volumes"] == ["./pooler-socket:/run/velaris-sock"]
        assert svc["environment"]["VELARIS_POOLER_SOCKET"] == "${VELARIS_CASE_DATABASE_POOLER_SOCKET:-}"
        # Root only to choose: PgBouncer itself never runs as root.
        assert 'exec setpriv --reuid="$$run_uid"' in script and "/bin/bash -c \"$$BASH_EXECUTION_STRING\"" in script
        assert "exec /usr/bin/pgbouncer" in script.split("BASH_EXECUTION_STRING", 1)[1]
        # Off: the image's own user, socket in /tmp — as before. On: the folder's owner, 0700, never root.
        assert 'run_uid="$$(id -u pgbouncer)"' in script and "POOLER_SOCK_DIR=/tmp" in script
        assert "chmod 0700 /run/velaris-sock" in script and 'if [ "$$run_uid" = 0 ]' in script
        assert "unix_socket_dir = $$POOLER_SOCK_DIR" in script


class TestWaitDepsSocket:
    def test_on_waits_for_the_socket_not_the_port(self, tmp_path, open_port):
        import stat
        envs = (f"VELARIS_CASE_DATABASE_URL=postgresql+asyncpg://v:p@127.0.0.1:{open_port}/v\n"
                f"VELARIS_CASE_DATABASE_POOLER_URL=postgresql+asyncpg://v:p@127.0.0.1:{open_port}/v\n"
                "VELARIS_CASE_DATABASE_POOLER_SOCKET=true\n")
        r = _wait_deps(tmp_path, envs)             # the port is open, the socket is not there
        assert r.returncode == 1 and "pooler-socket/.s.PGSQL.6432 still not there" in r.stderr
        d = tmp_path / "deploy/docker-compose/pooler-socket"
        d.mkdir(parents=True)
        os.mknod(d / ".s.PGSQL.6432", stat.S_IFSOCK | 0o600)
        r = _wait_deps(tmp_path, envs)
        assert r.returncode == 0 and "pooler-socket/.s.PGSQL.6432 is ready" in r.stdout


def test_setup_turns_the_socket_on_for_a_new_install_only():
    setup = (REPO / "setup-velaris.sh").read_text()
    block = setup.split('if ! grep -q "^VELARIS_CASE_DATABASE_POOLER_URL="', 1)[1].split("\nfi\n", 1)[0]
    assert 'echo "VELARIS_CASE_DATABASE_POOLER_SOCKET=true"' in block      # with the pooler, never alone
    for f in ("update-velaris.sh", "deploy/systemd/refresh-after-update.sh", "start-velaris.sh"):
        assert "VELARIS_CASE_DATABASE_POOLER_SOCKET=true" not in (REPO / f).read_text(), f


def test_a_twin_at_productions_socket_is_production():
    from case_service.hxdeploy import twin
    s = SimpleNamespace(database_url=DIRECT, database_pooler_url=POOLED, commerce_database_pooler_url="",
                        portal_database_pooler_url="", user_database_pooler_url="",
                        twin_database_url=backends.pooler_socket_url(POOLED), twin_task_queue="twin-q",
                        temporal_task_queue="prod-q")
    ok, detail = twin.isolation(s)
    assert not ok and detail["database"]["same"] is True
