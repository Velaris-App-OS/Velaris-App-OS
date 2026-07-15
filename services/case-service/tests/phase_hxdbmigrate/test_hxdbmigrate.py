"""HxDBMigrate P1 — source connector security + discovery report.

Unit tests (always run): source-type allowlist fail-closed, URL building/encoding, and the
SSRF guard (link-local/metadata blocked, private/loopback allowed — on-prem source DBs).
The live analyze flow is covered end-to-end when a MariaDB/MySQL/Postgres source is reachable
(gated on VELARIS_TEST_DATABASE_URL), otherwise skipped.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import pytest

from case_service.hxdbmigrate import report as R
from case_service.hxdbmigrate import source as S


# ── allowlist / URL (fail-closed) ─────────────────────────────────────────────

def test_source_types_allowlist():
    assert S.source_types() == ["mariadb", "mysql", "postgresql"]


@pytest.mark.parametrize("bad", ["mongodb", "sqlserver", "oracle", "sqlite", "", "../evil"])
def test_normalise_type_fail_closed(bad):
    with pytest.raises(S.SourceError):
        S.normalise_type(bad)


def test_mariadb_uses_mysql_driver():
    url = S._build_url("mariadb", "db.internal", 3306, "app", "u", "p")
    assert url.startswith("mysql+aiomysql://")


def test_url_encodes_credentials():
    url = S._build_url("postgresql", "h", 5432, "d", "u", "p@ss/w:rd#1")
    assert "p%40ss%2Fw%3Ard%231" in url and "@h:5432" in url


def test_default_ports():
    assert S.default_port("postgresql") == 5432
    assert S.default_port("mysql") == 3306 and S.default_port("mariadb") == 3306


# ── TLS / SSL modes ───────────────────────────────────────────────────────────

def test_ssl_disable_adds_no_ssl():
    assert "ssl" not in S._connect_args("postgresql", "disable")
    assert "ssl" not in S._connect_args("mysql", "disable")


def test_ssl_postgres_uses_asyncpg_mode_strings():
    assert S._connect_args("postgresql", "require")["ssl"] == "require"
    assert S._connect_args("postgresql", "verify")["ssl"] == "verify-full"


def test_ssl_mysql_require_disables_cert_verification():
    import ssl as _ssl
    ctx = S._connect_args("mysql", "require")["ssl"]
    assert isinstance(ctx, _ssl.SSLContext) and ctx.verify_mode == _ssl.CERT_NONE


def test_ssl_mysql_verify_enforces_cert_verification():
    import ssl as _ssl
    ctx = S._connect_args("mariadb", "verify")["ssl"]
    assert isinstance(ctx, _ssl.SSLContext) and ctx.verify_mode == _ssl.CERT_REQUIRED


def test_ssl_mode_fail_closed():
    with pytest.raises(S.SourceError):
        S._connect_args("postgresql", "bogus")


def test_ssl_modes_list():
    assert S.ssl_modes() == ["disable", "require", "verify"]


# ── SSRF guard (narrowed: block metadata/link-local, allow on-prem private) ────

def test_ssrf_blocks_link_local_metadata():
    with pytest.raises(S.SourceError):
        asyncio.run(S.validate_source_host("169.254.169.254"))


def test_ssrf_allows_loopback_and_private():
    # On-prem/self-hosted source DBs legitimately live on loopback / RFC1918.
    asyncio.run(S.validate_source_host("127.0.0.1"))
    asyncio.run(S.validate_source_host("10.0.0.5"))


def test_ssrf_rejects_empty_host():
    with pytest.raises(S.SourceError):
        asyncio.run(S.validate_source_host(""))


# ── quality scoring (deterministic) ───────────────────────────────────────────

def test_quality_flags_missing_pk_and_orphan_fk():
    schema = [
        {"table": "customers", "row_estimate": 2, "columns": [], "indexes": [{"is_primary": True}],
         "foreign_keys": [], "has_primary_key": True},
        {"table": "orders", "row_estimate": 2, "columns": [], "indexes": [{"is_primary": True}],
         "foreign_keys": [{"column_name": "ghost_id", "foreign_table": "ghost"}], "has_primary_key": True},
        {"table": "audit_log", "row_estimate": 0, "columns": [], "indexes": [],
         "foreign_keys": [], "has_primary_key": False},
    ]
    q = R._score_quality(schema, {"customers", "orders", "audit_log"})
    assert 0 <= q["score"] <= 100
    issues = " ".join(f["issue"] for f in q["findings"])
    assert "without a primary key" in issues       # audit_log
    assert "reference a missing table" in issues    # orders.ghost_id -> ghost


# ── report is JSON-safe (persisted as JSONB) ──────────────────────────────────

def test_report_is_json_serializable():
    # information_schema can return Decimal / datetime; the report must persist to JSONB.
    import json, datetime
    from decimal import Decimal
    raw = {"n": Decimal("255"), "f": Decimal("1.5"), "when": datetime.datetime(2026, 7, 2),
           "nested": [{"x": Decimal("3")}]}
    safe = R._jsonable(raw)
    json.dumps(safe)  # must not raise
    assert safe["n"] == 255 and safe["f"] == 1.5 and safe["nested"][0]["x"] == 3


# ── live end-to-end (gated on a reachable source) ─────────────────────────────

# Dedicated var (NOT VELARIS_TEST_DATABASE_URL, which conftest reserves for the platform
# harness DB and TRUNCATEs). The HxDBMigrate source is read-only and separate.
_SRC_ENV = "HXDBMIGRATE_TEST_SOURCE_URL"


def _source_from_env():
    url = os.environ.get(_SRC_ENV, "")
    if not url:
        return None
    sp = urlsplit(url)
    scheme = sp.scheme.split("+")[0]
    st = "mysql" if scheme == "mysql" else "postgresql"
    return (st, sp.hostname, sp.port, (sp.path or "/").lstrip("/"),
            sp.username or "root", sp.password or "")


@pytest.mark.skipif(not os.environ.get(_SRC_ENV),
                    reason="no external source DB configured (set HXDBMIGRATE_TEST_SOURCE_URL)")
def test_live_analyze_end_to_end():
    st, host, port, db, user, pw = _source_from_env()

    async def run():
        async with S.source_session(st, host, port, db, user, pw) as s:
            return await R.analyze_source(s)

    rep = asyncio.run(run())
    assert rep["table_count"] >= 1
    assert 0 <= rep["quality"]["score"] <= 100
    assert rep["autobiography"].startswith("# Schema Autobiography")
    assert isinstance(rep["schema"], list) and rep["schema"]
