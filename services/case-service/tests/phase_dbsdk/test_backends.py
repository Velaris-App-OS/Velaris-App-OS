"""DB SDK Phase 0 — first-party backend allowlist + URL resolution.

Pure unit tests (no DB). They prove the security-load-bearing behaviour:
  * backend selection is fail-closed (unknown name → SystemExit),
  * the contract is satisfied,
  * Phase 0 is behaviour-preserving (full database_url returned verbatim),
  * the forward-looking component-built URL path works and encodes credentials.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from case_service.db.backends import (
    ALLOWED_BACKENDS,
    MariadbBackend,
    MysqlBackend,
    PostgresBackend,
    get_backend,
    resolve_async_url,
    resolve_sync_url,
)


# ── allowlist / factory ───────────────────────────────────────────────────────

def test_allowlist_contents():
    assert set(ALLOWED_BACKENDS) == {"postgresql", "mysql", "mariadb"}


def test_get_backend_returns_postgres():
    assert isinstance(get_backend("postgresql"), PostgresBackend)


@pytest.mark.parametrize("name", ["  PostgreSQL  ", "POSTGRESQL", "postgresql"])
def test_get_backend_normalises_name(name):
    assert isinstance(get_backend(name), PostgresBackend)


@pytest.mark.parametrize("bad", ["sqlserver", "oracle", "", "sqlite", "mongodb", "../evil"])
def test_get_backend_fails_closed(bad):
    with pytest.raises(SystemExit):
        get_backend(bad)


def test_get_backend_returns_mysql():
    assert isinstance(get_backend("mysql"), MysqlBackend)


def test_postgres_satisfies_protocol_structurally():
    # The DatabaseBackend Protocol lives in helix_sdk.protocols (a stub package not
    # imported at runtime); assert structural conformance — every contract method exists.
    b = PostgresBackend()
    for method in (
        "name", "async_url", "sync_url", "migration_dialect", "driver_packages",
        "health_check", "initialize", "shutdown", "next_case_seq",
    ):
        assert callable(getattr(b, method, None)), f"missing contract method: {method}"


def test_postgres_descriptors():
    b = PostgresBackend()
    assert b.name() == "postgresql"
    assert b.migration_dialect() == "postgresql"
    assert "asyncpg" in b.driver_packages()


# ── URL building from typed components ────────────────────────────────────────

def _cfg(**kw):
    base = dict(
        database_backend="postgresql", database_url="",
        db_host="db.internal", db_port=6543, db_name="velaris",
        db_user="velaris", db_password="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_async_url_from_components():
    url = PostgresBackend().async_url(_cfg())
    assert url == "postgresql+asyncpg://velaris@db.internal:6543/velaris"


def test_sync_url_uses_psycopg2_driver():
    assert PostgresBackend().sync_url(_cfg()).startswith("postgresql+psycopg2://")


def test_component_defaults_applied():
    url = PostgresBackend().async_url(_cfg(db_host="", db_port=None, db_name="", db_user=""))
    assert url == "postgresql+asyncpg://helix@localhost:5432/helix"


def test_password_is_url_encoded():
    # @ : / # must not corrupt the URL.
    url = PostgresBackend().async_url(_cfg(db_password="p@ss/w:rd#1"))
    assert "p%40ss%2Fw%3Ard%231" in url
    assert "@db.internal" in url  # host separator intact


# ── resolve_* (the seam session.py uses) ──────────────────────────────────────

def test_resolve_returns_full_url_verbatim():
    """Phase 0 behaviour-preservation: a configured full URL is returned untouched."""
    full = "postgresql+asyncpg://u:p@h:5432/d"
    assert resolve_async_url(_cfg(database_url=full)) == full


def test_resolve_builds_when_url_empty():
    assert resolve_async_url(_cfg()) == "postgresql+asyncpg://velaris@db.internal:6543/velaris"


def test_resolve_sync_url():
    assert resolve_sync_url(_cfg()).startswith("postgresql+psycopg2://")


def test_resolve_fails_closed_on_unknown_backend():
    with pytest.raises(SystemExit):
        resolve_async_url(_cfg(database_backend="oracle"))


# ── MySQL backend + scheme pin ────────────────────────────────────────────────

def test_mysql_descriptors_and_options():
    b = MysqlBackend()
    assert b.name() == "mysql"
    assert b.migration_dialect() == "mysql"
    assert b.async_driver() == "mysql+aiomysql"
    assert "aiomysql" in b.driver_packages()
    # MySQL must run READ COMMITTED (defaults to REPEATABLE READ).
    assert b.engine_options().get("isolation_level") == "READ COMMITTED"


def test_postgres_engine_options_empty():
    # Byte-identical PG path depends on this staying empty.
    assert PostgresBackend().engine_options() == {}


def test_mysql_url_from_components():
    url = MysqlBackend().async_url(_cfg(database_backend="mysql"))
    assert url == "mysql+aiomysql://velaris@db.internal:6543/velaris"


def test_scheme_pin_accepts_matching_dialect():
    url = "mysql+aiomysql://u:p@h:3306/d"
    assert resolve_async_url(_cfg(database_backend="mysql", database_url=url)) == url


def test_scheme_pin_rejects_dialect_mismatch():
    # backend=mysql but a postgres URL → abort (the real footgun the pin guards).
    with pytest.raises(SystemExit):
        resolve_async_url(_cfg(database_backend="mysql",
                               database_url="postgresql+asyncpg://u:p@h:5432/d"))


# ── next_case_seq: per-dialect mechanism (SQL assertion via a recording session) ─

import asyncio


class _RecordingSession:
    """Minimal async session double: records executed SQL, returns canned scalars."""

    def __init__(self, scalars):
        self._scalars = list(scalars)
        self.sql = []

    async def execute(self, statement, *a, **k):
        self.sql.append(str(statement))
        val = self._scalars.pop(0)

        class _R:
            def scalar(self_inner):
                return val

        return _R()


def test_postgres_next_case_seq_uses_native_sequence():
    sess = _RecordingSession([42])
    val = asyncio.run(PostgresBackend().next_case_seq(sess))
    assert val == 42
    assert sess.sql == ["SELECT nextval('helix_case_seq')"]  # byte-identical to original


def test_mysql_next_case_seq_self_seeding_atomic():
    # First INSERT…ON DUPLICATE bumps the counter; SELECT LAST_INSERT_ID() reads it back.
    sess = _RecordingSession([None, 7])
    val = asyncio.run(MysqlBackend().next_case_seq(sess))
    assert val == 7
    assert len(sess.sql) == 2
    assert sess.sql[0].startswith("INSERT INTO velaris_sequences")
    assert "ON DUPLICATE KEY UPDATE value = LAST_INSERT_ID(value + 1)" in sess.sql[0]
    assert sess.sql[1] == "SELECT LAST_INSERT_ID()"


# ── MariaDB backend (first-class; reuses MySQL, distinct identity) ────────────

def test_get_backend_returns_mariadb():
    assert isinstance(get_backend("mariadb"), MariadbBackend)


def test_mariadb_is_a_mysql_backend():
    # It intentionally reuses the MySQL driver/baseline/seq — only identity differs.
    assert isinstance(MariadbBackend(), MysqlBackend)


def test_mariadb_descriptors():
    b = MariadbBackend()
    assert b.name() == "mariadb"                       # first-class, distinct from mysql
    assert b.migration_dialect() == "mysql"            # reuses migrations/mysql baseline
    assert b.async_driver() == "mysql+aiomysql"        # MariaDB reached via mysql driver
    assert b.sync_driver() == "mysql+pymysql"
    assert b.engine_options().get("isolation_level") == "READ COMMITTED"
    assert "aiomysql" in b.driver_packages()


def test_mariadb_url_from_components_uses_mysql_scheme():
    url = MariadbBackend().async_url(_cfg(database_backend="mariadb"))
    assert url == "mysql+aiomysql://velaris@db.internal:6543/velaris"


def test_scheme_pin_accepts_mysql_url_for_mariadb_backend():
    # MariaDB's SQLAlchemy scheme genuinely is `mysql`, so a mysql:// URL is valid
    # for the mariadb backend (the pin matches the driver scheme, not the name).
    url = "mysql+aiomysql://u:p@h:3306/d"
    assert resolve_async_url(_cfg(database_backend="mariadb", database_url=url)) == url


def test_scheme_pin_rejects_postgres_url_for_mariadb_backend():
    with pytest.raises(SystemExit):
        resolve_async_url(_cfg(database_backend="mariadb",
                               database_url="postgresql+asyncpg://u:p@h:5432/d"))
