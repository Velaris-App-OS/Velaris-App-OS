"""SqlQueryConnector — runs a SAVED read query against a tenant's database (2026-11-08).

Form Builder's third left-out item: a lookup or a choice field's options read
straight from the customer's own PostgreSQL / MySQL / MariaDB.

What keeps it safe (rules in velaris_shared.hxbridge.sql_query):
  * No SQL at run time. The executor passes the saved query of the operation a
    saved form field or step names (`_op_sql`); inputs only fill its `:name`
    bind parameters, sent to the driver as values.
  * The query is re-checked here before every run (one SELECT, nothing that
    writes), and runs in a READ ONLY transaction (PostgreSQL also gets
    `default_transaction_read_only` for the whole session) with a server-side
    time limit, an overall time limit and a row cap.
  * A tenant's connector connects only to a public address, pinned at connect
    time (as an HxDBMigrate source); the operator's may use its LAN.
  * Credentials {username, password} are stored encrypted, like every
    connector's; a read-only database account is still the right one to give.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import decimal
import json
import re
import uuid
from typing import Any

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector
from velaris_shared.hxbridge.sql_query import (
    NamedTLSContext, QueryError, _PARAM, _blank_literals, check_select, check_settings,
    verified_tls,
)

_MAX_VALUE_CHARS = 10_000


def _positional(sql: str, names: list[str], style: str) -> str:
    """`:name` → `$n` (PostgreSQL) or `%(name)s` (MySQL), found outside string
    literals only. For MySQL every literal `%` is doubled first, because the
    driver formats the whole statement."""
    bare = _blank_literals(sql)
    out, last = [], 0
    for m in _PARAM.finditer(bare):
        chunk = sql[last:m.start()]
        out.append(chunk.replace("%", "%%") if style == "mysql" else chunk)
        name = m.group(1)
        out.append(f"${names.index(name) + 1}" if style == "pg" else f"%({name})s")
        last = m.end()
    tail = sql[last:]
    out.append(tail.replace("%", "%%") if style == "mysql" else tail)
    return "".join(out)


def _input_value(val: Any) -> Any:
    """A form input as a bind value: a plain scalar, or its JSON text."""
    if val is None or isinstance(val, (bool, int, float)):
        return val
    if not isinstance(val, str):
        val = json.dumps(val, default=str)
    if len(val) > _MAX_VALUE_CHARS:
        raise RuntimeError(f"An input is longer than {_MAX_VALUE_CHARS} characters")
    return val


def _pg_coerce(val: Any, type_name: str) -> Any:
    """asyncpg takes each parameter in the type the server asks for, and form
    inputs arrive as text: convert, or refuse with a plain message."""
    if val is None:
        return None
    t = type_name.lower()
    try:
        if t in ("int2", "int4", "int8", "oid"):
            return int(val)
        if t in ("float4", "float8"):
            return float(val)
        if t == "numeric":
            return decimal.Decimal(str(val))
        if t == "bool":
            if isinstance(val, bool):
                return val
            s = str(val).strip().lower()
            if s in ("true", "t", "yes", "1"):
                return True
            if s in ("false", "f", "no", "0"):
                return False
            raise ValueError(val)
        if t == "date":
            return val if isinstance(val, _dt.date) else _dt.date.fromisoformat(str(val)[:10])
        if t in ("timestamp", "timestamptz"):
            d = _dt.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            if t == "timestamptz" and d.tzinfo is None:
                d = d.replace(tzinfo=_dt.timezone.utc)
            if t == "timestamp" and d.tzinfo is not None:
                d = d.replace(tzinfo=None)
            return d
        if t == "uuid":
            return uuid.UUID(str(val))
        if t in ("json", "jsonb"):
            return val if isinstance(val, str) else json.dumps(val)
    except (ValueError, TypeError, decimal.InvalidOperation):
        raise RuntimeError(f"Input {val!r} is not a valid {type_name}") from None
    return str(val) if not isinstance(val, str) else val


def _row_value(val: Any) -> Any:
    if isinstance(val, (bytes, bytearray, memoryview)):
        return bytes(val).hex()
    if isinstance(val, decimal.Decimal):
        return str(val)
    if isinstance(val, (_dt.date, _dt.datetime, _dt.time, uuid.UUID)):
        return val.isoformat() if hasattr(val, "isoformat") else str(val)
    return val


#: A value larger than this never leaves the database (it arrives as NULL),
#: and the whole result stays under the executor's overflow threshold — so a
#: tenant's query cannot fill this shared process's memory, and a lookup never
#: comes back as an overflow reference.
_MAX_CELL_BYTES = 16 * 1024
_MAX_COLUMNS = 100
_RESULT_BUDGET = 200 * 1024
_CHUNK = 20
_DQ, _BQ = '"', '`'
#: PostgreSQL types whose values are small and fixed in size.
_FIXED_PG = frozenset({"int2", "int4", "int8", "oid", "float4", "float8", "bool", "date",
                       "time", "timetz", "timestamp", "timestamptz", "uuid", "money", "interval"})


#: Kept under its first name; the class lives in velaris_shared (HxDBMigrate uses it too).
_NamedContext = NamedTLSContext


def _qi(name: str, quote: str) -> str:
    """A column name as a quoted identifier."""
    return quote + str(name).replace(quote, quote * 2) + quote


def _check_columns(columns: list[str]) -> None:
    if len(columns) > _MAX_COLUMNS:
        raise RuntimeError(f"A query returns at most {_MAX_COLUMNS} columns")
    if len(set(columns)) != len(columns):
        raise RuntimeError("Give every column of the query its own name (use AS)")


def _short(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:300]


async def _read(fetch, max_rows: int) -> tuple[list[dict], bool]:
    """Rows in small chunks until max_rows+1 or the size budget; (rows, cut)."""
    rows: list[dict] = []
    size = 0
    while len(rows) <= max_rows:
        chunk = await fetch(_CHUNK)
        if not chunk:
            return rows, False
        for r in chunk:
            row = {k: _row_value(v) for k, v in dict(r).items()}
            size += len(json.dumps(row, default=str))
            if size > _RESULT_BUDGET:
                return rows, True
            rows.append(row)
    return rows, False


@register_connector("sql_query")
class SqlQueryConnector(ConnectorProtocol):
    display_name = "Database query"
    description = ("Saved read-only SELECT queries on your own PostgreSQL, MySQL or MariaDB "
                   "database — forms call a query by name; inputs fill its :name parameters.")
    config_schema = {
        "engine": "postgresql | mysql | mariadb",
        "host": "str", "port": "int (default 5432 / 3306)", "database": "str",
        "ssl_mode": "disable | require (default) | verify",
        "timeout_seconds": "int 1–30 (default 10)", "max_rows": "int 1–1000 (default 100)",
        "operations": "[{key, label, sql: 'SELECT … WHERE email = :email'}]",
    }
    credential_schema = {"username": "str", "password": "str"}

    def __init__(self, config: dict, credentials: dict):
        self._config = config or {}
        self._creds = credentials or {}
        # Both set only by the executor (runtime_config / apply_operation).
        self._platform_owned = self._config.get("_platform_owned") is True
        sql = self._config.get("_op_sql")
        self._sql = sql if isinstance(sql, str) else None

    async def _address(self, settings: dict) -> str:
        """Where to connect: the operator's may be on the LAN (metadata and
        link-local still refused); a tenant's only the ONE public address its
        name resolved to — for verified TLS too: the certificate is still
        checked against the name (`_tls`), so nothing re-resolves it."""
        from case_service.hxdbmigrate.source import SourceError, pinned_public_address, validate_source_host
        host = settings["host"]
        try:
            await validate_source_host(host)
            if not self._platform_owned:
                host = await pinned_public_address(host)
        except SourceError as exc:
            raise RuntimeError(str(exc)) from None
        return host

    @staticmethod
    def _tls(settings: dict):
        """The TLS context: none, encrypted only, or verified against the
        connector's host NAME even when the socket goes to a pinned address."""
        import ssl as _ssl
        mode = settings["ssl_mode"]
        if mode == "disable":
            return None
        if mode == "require":
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            return ctx
        return verified_tls(settings["host"])

    async def execute(self, input_data: dict) -> dict:
        if not self._sql:
            raise RuntimeError("Name one of this connector's saved queries")
        try:
            settings = check_settings(self._config)
            names = check_select(self._sql)          # the saved text, checked again
        except QueryError as exc:
            raise RuntimeError(str(exc)) from None
        values = {n: _input_value((input_data or {}).get(n)) for n in names}
        run = self._run_pg if settings["engine"] == "postgresql" else self._run_mysql
        try:
            return await asyncio.wait_for(run(settings, names, values),
                                          timeout=settings["timeout_seconds"] + 10)
        except asyncio.TimeoutError:
            raise RuntimeError(f"The query took longer than {settings['timeout_seconds']} s") from None

    # ── PostgreSQL ──────────────────────────────────────────────────────────
    async def _pg_connect(self, settings: dict, host: str):
        import asyncpg
        ssl = self._tls(settings)
        return await asyncpg.connect(
            host=host, port=settings["port"], database=settings["database"],
            user=str(self._creds.get("username") or ""), password=str(self._creds.get("password") or ""),
            ssl=ssl if ssl is not None else False, timeout=10, statement_cache_size=0,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": str(settings["timeout_seconds"] * 1000),
                "idle_in_transaction_session_timeout": str((settings["timeout_seconds"] + 5) * 1000),
                "application_name": "velaris-sql-query",
            })

    async def _run_pg(self, settings: dict, names: list[str], values: dict) -> dict:
        sql = _positional(self._sql, names, "pg")
        try:
            conn = await self._pg_connect(settings, await self._address(settings))
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Could not connect to the database: {_short(exc)}") from None
        try:
            async with conn.transaction(readonly=True):
                # Described first (not run): the columns and parameter types.
                plain = await conn.prepare(sql)
                columns = [(a.name, a.type.name) for a in plain.get_attributes()]
                _check_columns([c for c, _t in columns])
                types = [t.name for t in plain.get_parameters()]
                args = [_pg_coerce(values[n], types[i]) for i, n in enumerate(names)]
                # Run wrapped: at most max_rows+1 rows, and no value over
                # _MAX_CELL_BYTES ever leaves the database (NULL in its place).
                cols = ", ".join(
                    f"q.{_qi(c, _DQ)}" if t in _FIXED_PG else
                    f"CASE WHEN octet_length(q.{_qi(c, _DQ)}::text) <= {_MAX_CELL_BYTES} "
                    f"THEN q.{_qi(c, _DQ)} END AS {_qi(c, _DQ)}"
                    for c, t in columns)
                wrapped = await conn.prepare(
                    f"SELECT {cols} FROM ({sql}) AS q LIMIT {settings['max_rows'] + 1}")
                cur = await wrapped.cursor(*args)
                rows = await _read(cur.fetch, settings["max_rows"])
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Database error: {_short(exc)}") from None
        finally:
            await conn.close(timeout=5)
        return self._result(rows, [c for c, _t in columns], settings["max_rows"])

    # ── MySQL / MariaDB ─────────────────────────────────────────────────────
    async def _run_mysql(self, settings: dict, names: list[str], values: dict) -> dict:
        try:
            import aiomysql
        except ImportError:
            raise RuntimeError("The MySQL driver is not installed on this platform") from None
        sql = _positional(self._sql, names, "mysql")
        host = await self._address(settings)
        try:
            conn = await aiomysql.connect(
                host=host, port=settings["port"], db=settings["database"],
                user=str(self._creds.get("username") or ""), password=str(self._creds.get("password") or ""),
                ssl=self._tls(settings), connect_timeout=10, autocommit=False, charset="utf8mb4",
                local_infile=False)
        except Exception as exc:
            raise RuntimeError(f"Could not connect to the database: {_short(exc)}") from None
        try:
            async with conn.cursor() as c:
                await c.execute("SET SESSION TRANSACTION READ ONLY")
                ms = settings["timeout_seconds"] * 1000
                try:
                    await c.execute(f"SET SESSION max_execution_time = {int(ms)}")       # MySQL
                except Exception:
                    await c.execute(f"SET SESSION max_statement_time = {int(ms) / 1000}")  # MariaDB
                await c.execute("START TRANSACTION READ ONLY")
                # The columns, without reading a row (`values` is always a dict,
                # so the driver always formats and doubled `%` become one).
                await c.execute(f"SELECT * FROM ({sql}) AS q LIMIT 0", values)
                columns = [d[0] for d in (c.description or [])]
            _check_columns(columns)
            cols = ", ".join(
                f"CASE WHEN LENGTH(q.{_qi(c, _BQ)}) <= {_MAX_CELL_BYTES} THEN q.{_qi(c, _BQ)} END "
                f"AS {_qi(c, _BQ)}" for c in columns)
            # Unbuffered: only the rows asked for are read from the server. Not
            # closed — closing it would read the rest; the socket is dropped.
            c = await conn.cursor(aiomysql.SSDictCursor)
            await c.execute(f"SELECT {cols} FROM ({sql}) AS q LIMIT {settings['max_rows'] + 1}", values)
            rows = await _read(c.fetchmany, settings["max_rows"])
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Database error: {_short(exc)}") from None
        finally:
            conn.close()           # drop the socket — never drain a large result
        return self._result(rows, columns, settings["max_rows"])

    @staticmethod
    def _result(read: tuple[list[dict], bool], columns: list[str], max_rows: int) -> dict:
        rows, cut_by_size = read
        truncated = cut_by_size or len(rows) > max_rows
        rows = rows[:max_rows]
        return {"rows": rows, "row": rows[0] if rows else None, "row_count": len(rows),
                "truncated": truncated, "columns": columns}

    async def test(self) -> bool:
        """Connect and run SELECT 1 read-only."""
        try:
            settings = check_settings(self._config)
        except QueryError:
            return False
        saved = self._sql
        self._sql = "SELECT 1 AS ok"
        try:
            run = self._run_pg if settings["engine"] == "postgresql" else self._run_mysql
            await asyncio.wait_for(run(settings, [], {}), timeout=settings["timeout_seconds"] + 10)
            return True
        except Exception:
            return False
        finally:
            self._sql = saved
