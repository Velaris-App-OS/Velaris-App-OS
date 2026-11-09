"""Database query connector — saved read-only SELECTs on a tenant's database (2026-11-08).

Form Builder's third left-out item. No SQL at run time: a saved form field
names a saved query, its inputs fill only bind parameters. A tenant's database
must be public (pinned at connect); the operator's may be on the LAN.

Live runs against a real database are opt-in: set VELARIS_TEST_SQL_PG
(`host:port:db:user:password`) and/or VELARIS_TEST_SQL_MYSQL the same way.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from tests.conftest import TestSessionFactory, _headers
from velaris_shared.db.models import (
    ConnectorRegistryModel, FormDefinitionModel, TenantModel, UserDirectoryModel,
)
from velaris_shared.hxbridge.operations import OperationError, apply_operation, validate_operations
from velaris_shared.hxbridge.sql_query import QueryError, check_select, check_settings

DB = {"engine": "postgresql", "host": "db.example.com", "database": "crm"}
Q = [{"key": "find_customer", "label": "Find customer",
      "sql": "SELECT id, name FROM customers WHERE email = :email"}]


# ── the rules ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "DELETE FROM customers",
    "UPDATE customers SET name = 'x'",
    "SELECT 1; DROP TABLE customers",
    "SELECT 1;; ",
    "WITH gone AS (DELETE FROM customers RETURNING *) SELECT * FROM gone",
    "SELECT * INTO copy_of FROM customers",
    "SELECT * FROM customers INTO OUTFILE '/tmp/x'",
    "SELECT * FROM customers FOR UPDATE",
    "SELECT * FROM customers FOR SHARE",
    "SELECT 1 -- hidden",
    "SELECT 1 /* hidden */",
    "SELECT 1 # hidden",
    "SELECT $$x$$",
    "SELECT 'unclosed",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT dblink_exec('x', 'DROP TABLE y')",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT nextval('seq')",
    "SELECT LOAD_FILE('/etc/passwd')",
    "",
    "x" * 10_001,
])
def test_only_one_read_query_is_accepted(sql):
    with pytest.raises(QueryError):
        check_select(sql)


def test_ordinary_reads_pass_and_their_parameters_are_found():
    assert check_select("SELECT id FROM t WHERE email = :email AND id = :id::int") == ["email", "id"]
    assert check_select("WITH a AS (SELECT 1 AS x) SELECT x FROM a;") == []
    # Words inside literals or quoted names are data, not SQL.
    assert check_select("SELECT \"update\", replace(note, 'delete', '') FROM t WHERE s = ':nope'") == []
    assert check_select("SELECT start, comment FROM t WHERE k = :k OR k2 = :k") == ["k"]


def test_database_settings_are_checked_with_safe_defaults():
    s = check_settings(DB)
    assert (s["port"], s["ssl_mode"], s["timeout_seconds"], s["max_rows"]) == (5432, "require", 10, 100)
    for bad in ({**DB, "engine": "oracle"}, {**DB, "host": ""}, {**DB, "host": "a b"},
                {**DB, "host": "u@h"}, {**DB, "database": ""}, {**DB, "ssl_mode": "maybe"},
                {**DB, "port": 0}, {**DB, "timeout_seconds": 31}, {**DB, "max_rows": 1001},
                {**DB, "max_rows": True}):
        with pytest.raises(QueryError):
            check_settings(bad)


def test_queries_are_operations_and_the_saved_sql_is_what_runs():
    ops = validate_operations("sql_query", {**DB, "operations": Q})
    assert ops[0]["params"] == ["email"] and ops[0]["method"] == "QUERY"
    cfg = apply_operation("sql_query", {**DB, "operations": Q}, "find_customer")
    assert cfg["_op_sql"] == Q[0]["sql"]
    assert "_op_sql" not in apply_operation("sql_query", {**DB, "operations": Q}, None)
    with pytest.raises(OperationError):
        apply_operation("sql_query", {**DB, "operations": Q}, "drop_everything")
    with pytest.raises(OperationError):           # a planted query never runs
        validate_operations("sql_query", {**DB, "_op_sql": "DELETE FROM customers"})
    assert "_op_sql" not in apply_operation("sql_query", {**DB, "_op_sql": "DELETE FROM x"}, None)
    with pytest.raises(OperationError):
        validate_operations("sql_query", {**DB, "operations": [{"key": "w", "sql": "DELETE FROM t"}]})
    with pytest.raises(OperationError):
        validate_operations("sql_query", {**DB, "operations": [Q[0], Q[0]]})


async def test_the_connector_checks_the_query_again_and_needs_one():
    from case_service.hxbridge.connectors.sql_query_connector import SqlQueryConnector
    with pytest.raises(RuntimeError, match="saved queries"):
        await SqlQueryConnector({**DB, "_platform_owned": False}, {}).execute({})
    # A query that slipped into the run config some other way is still refused.
    with pytest.raises(RuntimeError, match="only reads"):
        await SqlQueryConnector({**DB, "_op_sql": "WITH d AS (DELETE FROM t RETURNING 1) SELECT 1"},
                                {}).execute({})


async def test_a_tenants_connector_never_connects_inside(monkeypatch):
    """Even a name that resolves inward at connect time (DNS rebinding)."""
    import socket
    from case_service.hxbridge.connectors.sql_query_connector import SqlQueryConnector
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    conn = SqlQueryConnector({**DB, "_platform_owned": False,
                              "_op_sql": "SELECT 1"}, {"username": "u", "password": "p"})
    with pytest.raises(RuntimeError, match="private and internal"):
        await conn.execute({})


def test_bind_values_are_converted_for_postgresql_and_text_is_doubled_for_mysql():
    from case_service.hxbridge.connectors.sql_query_connector import _pg_coerce, _positional
    assert _pg_coerce("42", "int4") == 42 and _pg_coerce("true", "bool") is True
    assert str(_pg_coerce("2026-11-08", "date")) == "2026-11-08"
    with pytest.raises(RuntimeError):
        _pg_coerce("42; DROP TABLE x", "int4")
    sql = "SELECT name FROM t WHERE n LIKE '%a%' AND k = :k AND j = :k"
    assert _positional(sql, ["k"], "pg") == "SELECT name FROM t WHERE n LIKE '%a%' AND k = $1 AND j = $1"
    assert _positional(sql, ["k"], "mysql") == \
        "SELECT name FROM t WHERE n LIKE '%%a%%' AND k = %(k)s AND j = %(k)s"


async def test_verified_tls_is_pinned_and_checked_against_the_name(monkeypatch):
    """ssl_mode=verify kept the NAME for the socket, so a rebinding name could
    still open a connection inward; now the socket goes to the checked address
    and the certificate is checked against the name."""
    import socket
    from case_service.hxbridge.connectors.sql_query_connector import SqlQueryConnector, _NamedContext
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    settings = check_settings({**DB, "ssl_mode": "verify"})
    conn = SqlQueryConnector({**DB, "_platform_owned": False}, {})
    assert await conn._address(settings) == "93.184.216.34"
    ctx = conn._tls(settings)
    assert isinstance(ctx, _NamedContext) and ctx.velaris_name == "db.example.com" and ctx.check_hostname


async def test_reading_stops_at_the_size_budget_and_columns_are_checked():
    from case_service.hxbridge.connectors import sql_query_connector as mod
    big = [{"v": "x" * 15_000} for _ in range(100)]

    async def fetch(n, _rows=iter(big)):
        return [r for _, r in zip(range(n), _rows)]
    rows, cut = await mod._read(fetch, 1000)
    assert cut is True and 0 < len(rows) < 100
    assert sum(len(r["v"]) for r in rows) <= mod._RESULT_BUDGET
    with pytest.raises(RuntimeError, match="own name"):
        mod._check_columns(["id", "id"])
    with pytest.raises(RuntimeError, match="at most"):
        mod._check_columns([f"c{i}" for i in range(101)])
    assert mod._qi('a"b', '"') == '"a""b"' and mod._qi("a`b", "`") == "`a``b`"


async def test_a_dead_letter_retry_runs_the_same_operation(monkeypatch):
    from case_service.hxbridge import executor
    from velaris_shared.db.models import DeadLetterQueueModel
    seen = []

    async def fake_execute(session, connector, payload, **kw):
        seen.append((payload, kw.get("operation")))
        return {}
    async with TestSessionFactory() as db:
        c = ConnectorRegistryModel(name=f"db-{uuid.uuid4().hex[:6]}", connector_type="sql_query",
                                   config={**DB, "operations": Q}, credentials={}, tenant_id="default", enabled=True)
        db.add(c)
        await db.commit()
        # A failing run records its operation with the item — a planted key does not count.
        try:
            await executor.execute_connector(db, c, {"email": "a@b.test", executor.DLQ_OPERATION_KEY: "x"},
                                             operation="find_customer")
        except RuntimeError:
            pass
        from sqlalchemy import select
        item = (await db.execute(select(DeadLetterQueueModel).where(
            DeadLetterQueueModel.connector_id == c.id))).scalar_one()
        assert item.payload == {"email": "a@b.test", executor.DLQ_OPERATION_KEY: "find_customer"}
        monkeypatch.setattr(executor, "execute_connector", fake_execute)
        await executor.retry_dlq_item(db, item, c)
        assert seen == [({"email": "a@b.test"}, "find_customer")]


# ── saving: who may, and where to ─────────────────────────────────────────────

@pytest_asyncio.fixture
async def tenant_admin():
    suffix = uuid.uuid4().hex[:6]
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug=f"sqlt-{suffix}", name="SQL tenant")
        db.add_all([t, UserDirectoryModel(id=uuid.uuid4(), user_id=f"sqla-{suffix}", tenant_id=t.slug)])
        await db.commit()
    return _headers(f"sqla-{suffix}", ["admin"])


def _body(**cfg):
    return {"name": f"db-{uuid.uuid4().hex[:6]}", "connector_type": "sql_query",
            "config": {**DB, "operations": Q, **cfg}, "credentials": {"username": "ro", "password": "pw"}}


async def test_a_tenant_admin_saves_a_public_database_but_not_an_internal_one(client, tenant_admin, monkeypatch):
    import socket
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "db.example.com":
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    r = await client.post("/api/v1/hxbridge/connectors", headers=tenant_admin, json=_body())
    assert r.status_code == 201, r.text
    assert r.json()["operations"] == [{"key": "find_customer", "label": "Find customer", "method": "QUERY"}]
    for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "localhost"):
        r = await client.post("/api/v1/hxbridge/connectors", headers=tenant_admin, json=_body(host=host))
        assert r.status_code == 400, (host, r.text)
    r = await client.post("/api/v1/hxbridge/connectors", headers=tenant_admin, json=_body(
        operations=[{"key": "w", "sql": "DELETE FROM customers"}]))
    assert r.status_code == 400


async def test_the_operator_may_name_its_own_lan(client):
    r = await client.post("/api/v1/hxbridge/connectors", headers=_headers("sql-op", ["admin"]),
                          json=_body(host="127.0.0.1"))
    assert r.status_code == 201, r.text


async def test_a_case_worker_cannot_create_one(client):
    r = await client.post("/api/v1/hxbridge/connectors", headers=_headers("sql-w", ["case_worker"]),
                          json=_body())
    assert r.status_code == 403


# ── running: the saved form field chooses the query ───────────────────────────

async def test_a_form_runs_its_saved_query_with_only_its_mapped_inputs(client, monkeypatch):
    from case_service.hxbridge.connectors import sql_query_connector as mod
    seen = {}

    async def fake_pg(self, settings, names, values):
        seen.update(sql=self._sql, values=values)
        return {"rows": [{"id": 1}], "row": {"id": 1}, "row_count": 1, "truncated": False, "columns": ["id"]}
    monkeypatch.setattr(mod.SqlQueryConnector, "_run_pg", fake_pg)
    async with TestSessionFactory() as db:
        c = ConnectorRegistryModel(name=f"db-{uuid.uuid4().hex[:6]}", connector_type="sql_query",
                                   config={**DB, "host": "127.0.0.1", "operations": Q + [
                                       {"key": "all_rows", "sql": "SELECT * FROM customers"}]},
                                   credentials={}, tenant_id="default", enabled=True)
        db.add(c)
        await db.flush()
        f = FormDefinitionModel(name="Lookup", version="1.0.0", tenant_id=None, definition_json={
            "sections": [{"id": "s", "title": "S", "fields": [
                {"id": "who", "type": "connector_lookup", "label": "Who", "field_key": "who",
                 "source": {"connector_id": str(c.id), "operation": "find_customer",
                            "input_mapping": {"email": "email"}}}]}]})
        db.add(f)
        await db.commit()
    worker = _headers("sql-worker", ["case_worker"])
    r = await client.post(f"/api/v1/hxbridge/connectors/{c.id}/form-lookup", headers=worker, json={
        "input_data": {"email": "a@b.test", "sql": "DELETE FROM customers", "other": "x"},
        "form_id": str(f.id), "field_key": "who", "operation": "all_rows"})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["row"] == {"id": 1}
    assert seen == {"sql": Q[0]["sql"], "values": {"email": "a@b.test"}}
    # Not part of a saved form: refused.
    r = await client.post(f"/api/v1/hxbridge/connectors/{c.id}/form-lookup", headers=worker,
                          json={"input_data": {}, "operation": "all_rows"})
    assert r.status_code == 403


# ── live (opt-in) ─────────────────────────────────────────────────────────────

def _live(var):
    raw = os.environ.get(var)
    if not raw:
        pytest.skip(f"{var} not set")
    host, port, db, user, pw = raw.split(":", 4)
    return {"host": host, "port": int(port), "database": db, "ssl_mode": "disable",
            "max_rows": 2, "timeout_seconds": 2}, {"username": user, "password": pw}


@pytest.mark.parametrize("var,engine", [("VELARIS_TEST_SQL_PG", "postgresql"),
                                        ("VELARIS_TEST_SQL_MYSQL", "mysql")])
async def test_live_read_cap_timeout_and_read_only(var, engine):
    from case_service.hxbridge.connectors.sql_query_connector import SqlQueryConnector
    cfg, creds = _live(var)
    cfg["engine"] = engine

    def run(sql, data=None):
        return SqlQueryConnector({**cfg, "_platform_owned": True, "_op_sql": sql}, creds).execute(data or {})
    out = await run("SELECT name, n FROM vx_people WHERE name LIKE '%a%' AND n >= :min ORDER BY n",
                    {"min": "1"})
    assert out["row_count"] == 2 and out["truncated"] is True and out["columns"] == ["name", "n"]
    one = await run("SELECT name FROM vx_people WHERE name = :who", {"who": "x' OR '1'='1"})
    assert one["rows"] == []                                     # a value, never SQL
    sleep = "SELECT pg_sleep(5)" if engine == "postgresql" else "SELECT SLEEP(5) AS s FROM vx_people"
    with pytest.raises(RuntimeError):
        r = await run(sleep)
        if engine != "postgresql":       # MySQL's limit ends SLEEP early with 1, not an error
            assert all(v == 1 for row in r["rows"] for v in row.values())
            raise RuntimeError("stopped")
    assert await SqlQueryConnector({**cfg, "_platform_owned": True}, creds).test() is True
    # A huge value never leaves the database; a CTE query is wrapped too.
    big = "SELECT repeat('x', 5000000) AS v, 1 AS n" if engine == "postgresql" else \
          "SELECT REPEAT('x', 5000000) AS v, 1 AS n"
    out = await run(big)
    assert out["rows"] == [{"v": None, "n": 1}]
    out = await run("WITH a AS (SELECT name FROM vx_people WHERE n = :n) SELECT name FROM a", {"n": "2"})
    assert out["rows"] == [{"name": "maria"}]
    with pytest.raises(RuntimeError, match="own name|Duplicate column"):   # MySQL refuses it itself
        await run("SELECT name, name FROM vx_people")
