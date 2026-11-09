"""Heavy parts on other servers (Nov_TODO R5e item 5) — the client side.

What would be silently wrong: a bundled install (nothing set) getting anything
other than exactly what it had — the same URL string, no connect_args,
plaintext Temporal, no Ollama header, SASL_PLAINTEXT — and, the other way, a
TLS setting that is accepted but quietly not applied.

The shell side (deploy/external-services.sh: the list, the refusals, the
compose service list) is exercised through bash at the end.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
import ssl
import subprocess
from pathlib import Path

import pytest

from velaris_shared.config import get_settings
from velaris_shared.db.tls import split_tls

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def pki(tmp_path_factory) -> dict[str, str]:
    """A throwaway CA + a client certificate signed by it (PEM files)."""
    d = tmp_path_factory.mktemp("pki")
    def sh(*a):
        subprocess.run(a, cwd=d, check=True, capture_output=True)
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
       "-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=test-ca")
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "client.key",
       "-out", "client.csr", "-subj", "/CN=velaris")
    sh("openssl", "x509", "-req", "-in", "client.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
       "-CAcreateserial", "-days", "1", "-out", "client.pem")
    return {k: str(d / f) for k, f in
            (("ca", "ca.pem"), ("cert", "client.pem"), ("key", "client.key"))}


@pytest.fixture
def settings_env(monkeypatch):
    """Set VELARIS_CASE_* for one test; the settings cache is cleared around it."""
    def _set(**kv):
        for k, v in kv.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


# ── database URL ──────────────────────────────────────────────────────────────

class TestSplitTls:
    @pytest.mark.parametrize("url", [
        "postgresql+asyncpg://velaris:p%40ss@localhost:5432/velaris",
        "postgresql+asyncpg://velaris:pw@localhost:5432/velaris?application_name=x",
        "mysql+aiomysql://velaris:pw@db:3306/velaris",
        "sqlite+aiosqlite:///:memory:",
    ])
    def test_url_without_tls_is_untouched(self, url):
        assert split_tls(url) == (url, {})

    def test_verify_full_with_ca_becomes_a_checking_context(self, pki):
        url, args = split_tls(
            "postgresql+asyncpg://velaris:p%40ss@db.example:5432/velaris"
            f"?sslmode=verify-full&sslrootcert={pki['ca']}&application_name=x")
        assert url == "postgresql+asyncpg://velaris:p%40ss@db.example:5432/velaris?application_name=x"
        ctx = args["ssl"]
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.get_ca_certs()[0]["subject"] == ((("commonName", "test-ca"),),)

    def test_asyncpg_spelling_is_accepted(self, pki):
        _, args = split_tls(f"postgresql+asyncpg://u:p@h/db?ssl=verify-full&sslrootcert={pki['ca']}")
        assert args["ssl"].check_hostname is True

    def test_require_with_ca_verifies_the_chain_like_libpq(self, pki):
        _, args = split_tls(f"postgresql+asyncpg://u:p@h/db?sslmode=require&sslrootcert={pki['ca']}")
        assert args["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert args["ssl"].check_hostname is False

    def test_client_certificate_is_loaded(self, pki):
        _, args = split_tls(f"postgresql+asyncpg://u:p@h/db?sslmode=verify-full&sslrootcert={pki['ca']}"
                            f"&sslcert={pki['cert']}&sslkey={pki['key']}")
        assert isinstance(args["ssl"], ssl.SSLContext)

    def test_plain_mode_is_passed_by_name(self):
        assert split_tls("postgresql+asyncpg://u:p@h/db?sslmode=require") == (
            "postgresql+asyncpg://u:p@h/db", {"ssl": "require"})

    def test_mysql_ca_becomes_a_context(self, pki):
        url, args = split_tls(f"mysql+aiomysql://u:p@db:3306/velaris?ssl_ca={pki['ca']}&charset=utf8mb4")
        assert url == "mysql+aiomysql://u:p@db:3306/velaris?charset=utf8mb4"
        assert args["ssl"].check_hostname is True

    def test_missing_ca_file_fails_loudly(self):
        with pytest.raises(FileNotFoundError):
            split_tls("postgresql+asyncpg://u:p@h/db?sslmode=verify-full&sslrootcert=/nonexistent/ca.pem")


# ── Temporal ──────────────────────────────────────────────────────────────────

class TestTemporal:
    def test_defaults_are_the_bundled_plaintext_temporal(self, settings_env, monkeypatch):
        from velaris_shared import temporal_connect as tc
        monkeypatch.delenv("TEMPORAL_HOST", raising=False)
        monkeypatch.delenv("VELARIS_CASE_TEMPORAL_HOST", raising=False)
        settings_env()
        assert tc.temporal_target() == "localhost:7233"
        assert tc.temporal_tls() is False

    def test_temporal_host_still_wins(self, settings_env, monkeypatch):
        from velaris_shared import temporal_connect as tc
        monkeypatch.setenv("TEMPORAL_HOST", "temporal:7233")
        settings_env(VELARIS_CASE_TEMPORAL_HOST="elsewhere:7233")
        assert tc.temporal_target() == "temporal:7233"

    def test_the_documented_setting_is_now_read(self, settings_env, monkeypatch):
        from velaris_shared import temporal_connect as tc
        monkeypatch.delenv("TEMPORAL_HOST", raising=False)
        settings_env(VELARIS_CASE_TEMPORAL_HOST="temporal.example:7233")
        assert tc.temporal_target() == "temporal.example:7233"

    def test_mutual_tls(self, settings_env, pki):
        from velaris_shared import temporal_connect as tc
        settings_env(VELARIS_CASE_TEMPORAL_TLS_CA=pki["ca"], VELARIS_CASE_TEMPORAL_TLS_CERT=pki["cert"],
                     VELARIS_CASE_TEMPORAL_TLS_KEY=pki["key"],
                     VELARIS_CASE_TEMPORAL_TLS_SERVER_NAME="temporal.example")
        t = tc.temporal_tls()
        assert t.server_root_ca_cert == Path(pki["ca"]).read_bytes()
        assert t.client_cert and t.client_private_key and t.domain == "temporal.example"

    @pytest.mark.parametrize("which", [("CERT",), ("KEY",), ("CERT", "KEY")])
    def test_half_configured_is_refused(self, settings_env, pki, which):
        from velaris_shared import temporal_connect as tc
        settings_env(**{f"VELARIS_CASE_TEMPORAL_TLS_{w}": pki[w.lower()] for w in which})
        with pytest.raises(ValueError):
            tc.temporal_tls()


# ── event bus, Ollama ─────────────────────────────────────────────────────────

class TestEventBusTls:
    def test_no_ca_is_unchanged(self):
        from case_service.integrations.event_bus import _auth_kwargs
        assert _auth_kwargs("velaris", "pw")["security_protocol"] == "SASL_PLAINTEXT"
        assert _auth_kwargs("velaris", "") == {}

    def test_ca_turns_on_sasl_ssl(self, pki):
        pytest.importorskip("aiokafka")
        from case_service.integrations.event_bus import _auth_kwargs
        kw = _auth_kwargs("velaris", "pw", pki["ca"])
        assert kw["security_protocol"] == "SASL_SSL"
        assert kw["ssl_context"].check_hostname is True
        assert _auth_kwargs("velaris", "", pki["ca"])["security_protocol"] == "SSL"


class TestOllamaKey:
    def test_no_key_no_header(self, settings_env, monkeypatch):
        from velaris_shared.llm.ollama_backend import ollama_headers
        monkeypatch.delenv("VELARIS_CASE_AI_OLLAMA_API_KEY", raising=False)
        settings_env()
        assert ollama_headers() == {}

    def test_key_is_sent_as_bearer(self, settings_env):
        from velaris_shared.llm.ollama_backend import ollama_headers
        settings_env(VELARIS_CASE_AI_OLLAMA_API_KEY="k123")
        assert ollama_headers() == {"Authorization": "Bearer k123"}

    def test_key_never_in_settings_repr(self, settings_env):
        settings_env(VELARIS_CASE_AI_OLLAMA_API_KEY="k123")
        assert "k123" not in repr(get_settings())


# ── deploy/external-services.sh ───────────────────────────────────────────────

def _sh(env_lines: str, script: str, tmp_path: Path) -> subprocess.CompletedProcess:
    (tmp_path / ".env").write_text(env_lines)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VELARIS_", "TEMPORAL_"))}
    env["VELARIS_DIR"] = str(tmp_path)
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail; . '{REPO}/deploy/external-services.sh'; {script}"],
        env=env, capture_output=True, text=True)


class TestShell:
    @pytest.mark.parametrize("env_lines", ["", "VELARIS_EXTERNAL_SERVICES=\n", "OTHER=1\n"])
    def test_unset_or_empty_is_all_bundled(self, tmp_path, env_lines):
        r = _sh(env_lines, 'echo "[$(velaris_external_list)][$(velaris_external_compose_services)]"; '
                           'velaris_external_check; velaris_is_external database || echo bundled',
                tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["[][]", "bundled"]

    def test_unknown_part_is_refused(self, tmp_path):
        r = _sh("VELARIS_EXTERNAL_SERVICES=databse\n", "velaris_external_check", tmp_path)
        assert r.returncode == 1 and "unknown part 'databse'" in r.stderr

    def test_plaintext_parts_are_refused_by_name(self, tmp_path):
        r = _sh("VELARIS_EXTERNAL_SERVICES=database,temporal,events,ai,meetings\n"
                "VELARIS_CASE_DATABASE_URL=postgresql+asyncpg://u:p@db:5432/v\n"
                "VELARIS_CASE_AI_OLLAMA_URL=http://ai:11434\n"
                "VELARIS_CASE_LIVEKIT_URL=ws://meet:7880\n",
                "velaris_external_check", tmp_path)
        assert r.returncode == 1
        for needle in ("sslmode=verify-full", "VELARIS_CASE_TEMPORAL_TLS_CA", "VELARIS_CASE_TEMPORAL_TLS_KEY",
                       "VELARIS_CASE_EVENT_BUS_TLS_CA", "VELARIS_CASE_EVENT_BUS_PASSWORD",
                       "must be https://", "VELARIS_CASE_AI_OLLAMA_API_KEY", "must be wss://"):
            assert needle in r.stderr, needle

    def test_secured_parts_pass(self, tmp_path, pki):
        r = _sh("VELARIS_EXTERNAL_SERVICES=database,temporal,events,ai,meetings\n"
                f'VELARIS_CASE_DATABASE_URL="postgresql+asyncpg://u:p@db:5432/v?sslmode=verify-full&sslrootcert={pki["ca"]}"\n'
                f"VELARIS_CASE_TEMPORAL_TLS_CA={pki['ca']}\nVELARIS_CASE_TEMPORAL_TLS_CERT={pki['cert']}\n"
                f"VELARIS_CASE_TEMPORAL_TLS_KEY={pki['key']}\n"
                f"VELARIS_CASE_EVENT_BUS_TLS_CA={pki['ca']}\nVELARIS_CASE_EVENT_BUS_PASSWORD=x\n"
                "VELARIS_CASE_AI_OLLAMA_URL=https://ai.example\nVELARIS_CASE_AI_OLLAMA_API_KEY=k\n"
                "VELARIS_CASE_LIVEKIT_URL=wss://meet.example\n",
                "velaris_external_check && velaris_external_compose_services", tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["velaris-db", "velaris-pooler", "temporal", "temporal-db", "temporal-ui",
                                    "events", "ollama", "livekit", "livekit-egress"]

    def test_pg_url_for_libpq_and_mounts(self, tmp_path, pki):
        r = _sh(f"VELARIS_EXTERNAL_SERVICES=database\nVELARIS_CASE_DATABASE_URL="
                f"postgresql+asyncpg://u:p@db:5432/v?ssl=verify-full&sslrootcert={pki['ca']}\n",
                "velaris_pg_url; echo; _vx_pg_mounts", tmp_path)
        lines = r.stdout.splitlines()
        assert lines[0] == f"postgresql://u:p@db:5432/v?sslmode=verify-full&sslrootcert={pki['ca']}"
        assert lines[1:] == ["-v", f"{pki['ca']}:{pki['ca']}:ro"]

    def test_mysql_needs_a_ca(self, tmp_path, pki):
        base = "VELARIS_EXTERNAL_SERVICES=database\nVELARIS_CASE_DATABASE_BACKEND=mysql\n"
        r = _sh(base + "VELARIS_CASE_DATABASE_URL=mysql+aiomysql://u:p@db:3306/v\n",
                "velaris_external_check", tmp_path)
        assert r.returncode == 1 and "ssl_ca" in r.stderr
        r = _sh(base + f"VELARIS_CASE_DATABASE_URL=mysql+aiomysql://u:p@db:3306/v?ssl_ca={pki['ca']}\n",
                "velaris_external_check && velaris_mysql_tls_flags", tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["--ssl-mode=VERIFY_IDENTITY", f"--ssl-ca={pki['ca']}"]


# ── review fixes (2026-10-17) ─────────────────────────────────────────────────

class TestReviewFixes:
    def test_disable_stays_off_even_with_a_ca(self, pki):
        assert split_tls(f"postgresql+asyncpg://u:p@h/db?sslmode=disable&sslrootcert={pki['ca']}")[1] \
            == {"ssl": "disable"}

    def test_sslrootcert_system_means_the_system_cas(self):
        _, args = split_tls("postgresql+asyncpg://u:p@h/db?sslmode=verify-full&sslrootcert=system")
        assert args["ssl"].check_hostname is True and args["ssl"].get_ca_certs() is not None

    def test_repeated_key_last_wins(self, pki):
        _, args = split_tls(f"postgresql+asyncpg://u:p@h/db?sslmode=require&sslmode=verify-full"
                            f"&sslrootcert={pki['ca']}")
        assert args["ssl"].check_hostname is True

    def test_temporal_env_first_without_settings(self, monkeypatch, pki):
        """A pod given only these variables connects even if settings cannot load."""
        from velaris_shared import temporal_connect as tc
        monkeypatch.delenv("TEMPORAL_HOST", raising=False)
        monkeypatch.setenv("VELARIS_CASE_TEMPORAL_HOST", "t.example:7233")
        monkeypatch.setenv("VELARIS_CASE_TEMPORAL_TLS_CA", pki["ca"])
        monkeypatch.setenv("VELARIS_CASE_TEMPORAL_TLS_CERT", pki["cert"])
        monkeypatch.setenv("VELARIS_CASE_TEMPORAL_TLS_KEY", pki["key"])
        monkeypatch.setattr("velaris_shared.config.get_settings",
                            lambda: (_ for _ in ()).throw(RuntimeError("no settings")))
        assert tc.temporal_target() == "t.example:7233"
        assert tc.temporal_tls().client_cert

    def test_backup_libpq_url(self):
        import sys
        sys.path.insert(0, str(REPO / "scripts" / "backup"))
        import velaris_backup as vb
        assert vb._libpq_url("postgresql+asyncpg://u:p@db/v?ssl=verify-full&prepared_statement_cache_size=0") \
            == "postgresql://u:p@db/v?sslmode=verify-full&sslrootcert=system"
        assert vb._libpq_url("postgresql+asyncpg://u:p@db/v") == "postgresql://u:p@db/v"

    def test_backup_client_keeps_password_off_the_command_line(self):
        import sys
        sys.path.insert(0, str(REPO / "scripts" / "backup"))
        import velaris_backup as vb

        class C:
            external = {"database"}
            pg_url = vb._libpq_url("postgresql+asyncpg://u:p%40ss@x@db:5432/v?sslmode=verify-ca&sslrootcert=/c/ca.pem")
        argv, env = vb.pg_cmd(C(), "pg_dump")
        assert not any("ss@x" in a or "p%40" in a for a in argv)
        assert env["PGPASSWORD"] == "p@ss@x"
        assert env["VELARIS_PGURL"] == "postgresql://u@db:5432/v?sslmode=verify-ca&sslrootcert=/c/ca.pem"
        assert "/c/ca.pem:/c/ca.pem:ro" in argv


class TestShellReviewFixes:
    def test_pg_url_keeps_libpq_options_only(self, tmp_path):
        r = _sh("VELARIS_CASE_DATABASE_URL=postgresql+asyncpg://u:p@db/v?ssl=verify-full"
                "&prepared_statement_cache_size=0&application_name=velaris\n", "velaris_pg_url", tmp_path)
        assert r.stdout == "postgresql://u:p@db/v?sslmode=verify-full&application_name=velaris&sslrootcert=system"

    @pytest.mark.parametrize("url,password", [
        ("postgresql+asyncpg://u:p@ss:x@db.example:5432/v", "p@ss:x"),      # raw '@' in the password
        ("postgresql+asyncpg://u:a%5Cnb%25@db/v", "a\\nb%"),                 # encoded backslash + percent
        ("postgresql+asyncpg://u:x\\y@db/v", "x\\y"),                        # raw backslash stays
        ("postgresql+asyncpg://db/v", ""),
    ])
    def test_client_password_split(self, tmp_path, url, password):
        r = _sh(f"VELARIS_CASE_DATABASE_URL={url}\n",
                'docker(){ printf "%s|%s" "$PGPASSWORD" "$VELARIS_PGURL"; }; _vx_pg_client psql', tmp_path)
        assert r.returncode == 0, r.stderr
        got_pw, got_url = r.stdout.split("|", 1)
        assert got_pw == password
        assert "@" not in got_url.split("://", 1)[1].split("@")[0] or got_url.count("@") == 1
        assert password == "" or password not in got_url

    def test_mysql_hostname_check_cannot_be_switched_off(self, tmp_path, pki):
        r = _sh("VELARIS_EXTERNAL_SERVICES=database\nVELARIS_CASE_DATABASE_BACKEND=mysql\n"
                f"VELARIS_CASE_DATABASE_URL=mysql+aiomysql://u:p@db/v?ssl_ca={pki['ca']}&ssl_check_hostname=false\n",
                "velaris_external_check", tmp_path)
        assert r.returncode == 1 and "ssl_check_hostname" in r.stderr

    def test_no_password_invented_for_an_external_broker(self, tmp_path):
        r = _sh("VELARIS_EXTERNAL_SERVICES=events\nVELARIS_CASE_EVENT_BUS_ENABLED=true\n",
                f". '{REPO}/deploy/docker-compose/compose-profiles.sh'; velaris_ensure_event_bus_password; "
                "grep -c EVENT_BUS_PASSWORD \"$VELARIS_DIR/.env\" || true", tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().endswith("0")

    def test_unreadable_compose_list_is_an_error_not_start_everything(self, tmp_path):
        r = _sh("VELARIS_EXTERNAL_SERVICES=ai\n",
                "docker(){ echo boom >&2; return 1; }; velaris_compose_up_services f e", tmp_path)
        assert r.returncode == 1 and r.stdout == ""


def test_unquoted_ampersand_in_database_url_is_refused(tmp_path, pki):
    url = f"postgresql+asyncpg://u:p@db/v?sslmode=verify-ca&sslrootcert={pki['ca']}"
    r = _sh(f"VELARIS_EXTERNAL_SERVICES=database\nVELARIS_CASE_DATABASE_URL={url}\n",
            "velaris_external_check", tmp_path)
    assert r.returncode == 1 and "unquoted" in r.stderr
    r = _sh(f'VELARIS_EXTERNAL_SERVICES=database\nVELARIS_CASE_DATABASE_URL="{url}"\n',
            "velaris_external_check", tmp_path)
    assert r.returncode == 0, r.stderr
