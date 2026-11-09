"""Database query connector — the rules a saved query must pass (2026-11-08).

A `sql_query` connector reads a tenant's own PostgreSQL / MySQL / MariaDB
database. Its config names the database and lists SAVED queries as its
operations:

    {"engine": "postgresql", "host": "db.example.com", "port": 5432,
     "database": "crm", "ssl_mode": "require",
     "timeout_seconds": 10, "max_rows": 100,
     "operations": [
       {"key": "find_customer", "label": "Find customer",
        "sql": "SELECT id, name, email FROM customers WHERE email = :email"}
     ]}

Credentials (encrypted at rest): {"username": "...", "password": "..."}.

No SQL is ever taken at run time: a form or step names a saved query by key,
and its inputs only fill the query's `:name` bind parameters — values, never
SQL. A saved query is one SELECT (or WITH … SELECT): no second statement, no
comment, no write keyword, no `SELECT … INTO`, no row lock. The connector also
runs it in a READ ONLY transaction with a time limit and a row cap, so this
check is the first fence, not the only one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import ssl
from typing import Any

ENGINES = ("postgresql", "mysql", "mariadb")
DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306}
SSL_MODES = ("disable", "require", "verify")
MAX_SQL_CHARS = 10_000
TIMEOUT_DEFAULT, TIMEOUT_MAX = 10, 30
ROWS_DEFAULT, ROWS_MAX = 100, 1000

#: Words a saved read query never needs. The query is one statement starting
#: with SELECT/WITH, so a write can only hide in a data-modifying CTE
#: (`WITH x AS (DELETE …)`), `SELECT … INTO`, or a function with side effects —
#: those are what is listed. Matched as whole words OUTSIDE string literals and
#: quoted identifiers: a column "updated_at", a value 'delete me' or a quoted
#: "update" column is fine. Ordinary words (start, comment, set…) stay usable.
_FORBIDDEN = frozenset({
    "insert", "update", "delete", "merge", "upsert", "truncate", "drop", "alter",
    "create", "grant", "revoke", "into", "outfile", "dumpfile", "load_file",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_unlink", "dblink", "dblink_exec", "dblink_connect",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "set_config", "pg_advisory_lock", "pg_advisory_xact_lock", "get_lock",
    "nextval", "setval", "sys_exec", "sys_eval",
})
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
#: `:name` — not a PostgreSQL cast (`x::int`), which may follow one (`:id::int`).
_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]{0,39})(?!\w)")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


class QueryError(ValueError):
    """A saved query or the database settings are not acceptable."""


def _blank_literals(sql: str) -> str:
    """The query with string literals and quoted identifiers blanked out, so
    the checks below see only the SQL itself. Refuses what cannot be read
    safely: comments, PostgreSQL dollar quoting, an unclosed quote."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            j = i + 1
            while True:
                if j >= n:
                    raise QueryError("The query has an unclosed quote")
                if sql[j] == "\\" and c == "'":          # MySQL backslash escape
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:    # doubled quote = escaped
                        j += 2
                        continue
                    break
                j += 1
            out.append(c + " " * (j - i - 1) + c)
            i = j + 1
            continue
        if sql.startswith("--", i) or sql.startswith("/*", i) or c == "#":
            raise QueryError("Comments are not allowed in a saved query")
        if c == "$" and re.match(r"\$[A-Za-z_]*\$", sql[i:]):
            raise QueryError("Dollar-quoted text is not allowed in a saved query")
        out.append(c)
        i += 1
    return "".join(out)


def check_select(sql: Any) -> list[str]:
    """Raise QueryError unless `sql` is one read-only SELECT; return the names
    of its `:name` bind parameters, in order of first use."""
    if not isinstance(sql, str) or not sql.strip():
        raise QueryError("A query is required")
    if len(sql) > MAX_SQL_CHARS:
        raise QueryError(f"A query is at most {MAX_SQL_CHARS} characters")
    if "\x00" in sql:
        raise QueryError("The query contains a NUL character")
    bare = _blank_literals(sql).strip()
    if bare.endswith(";"):
        bare = bare[:-1].rstrip()
    if ";" in bare:
        raise QueryError("A saved query is one statement — no ';' inside it")
    words = [w.lower() for w in _WORD.findall(bare)]
    if not words or words[0] not in ("select", "with"):
        raise QueryError("A saved query starts with SELECT or WITH")
    bad = sorted(set(words) & _FORBIDDEN)
    if bad:
        raise QueryError(f"A saved query only reads — not allowed: {', '.join(bad).upper()}")
    if re.search(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b", bare, re.I):
        raise QueryError("A saved query may not lock rows (FOR UPDATE / FOR SHARE)")
    names: list[str] = []
    for m in _PARAM.finditer(bare):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def _int(cfg: dict, key: str, default: int, lo: int, hi: int) -> int:
    val = cfg.get(key, default)
    if val is None:
        return default
    if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
        raise QueryError(f"{key} must be a whole number from {lo} to {hi}")
    return val


def check_settings(config: dict | None) -> dict:
    """The database settings, checked and with defaults filled in."""
    cfg = config or {}
    engine = str(cfg.get("engine", "")).strip().lower()
    if engine not in ENGINES:
        raise QueryError(f"engine must be one of {', '.join(ENGINES)}")
    host = cfg.get("host")
    if not isinstance(host, str) or not host.strip() or len(host) > 255 \
            or re.search(r"[\s/@?#{}]", host.strip()):
        raise QueryError("host must be a host name or address")
    database = cfg.get("database")
    if not isinstance(database, str) or not database.strip() or len(database) > 128 \
            or re.search(r"[\s/?#@]", database):
        raise QueryError("database must be a database name")
    ssl_mode = str(cfg.get("ssl_mode") or "require").strip().lower()
    if ssl_mode not in SSL_MODES:
        raise QueryError(f"ssl_mode must be one of {', '.join(SSL_MODES)}")
    return {
        "engine": engine, "host": host.strip(), "database": database,
        "port": _int(cfg, "port", DEFAULT_PORTS[engine], 1, 65535),
        "ssl_mode": ssl_mode,
        "timeout_seconds": _int(cfg, "timeout_seconds", TIMEOUT_DEFAULT, 1, TIMEOUT_MAX),
        "max_rows": _int(cfg, "max_rows", ROWS_DEFAULT, 1, ROWS_MAX),
    }


def check_query_operation(op: dict) -> dict:
    """One saved query, checked: {key, label, sql, params}."""
    key = op.get("key")
    if not isinstance(key, str) or not _KEY.match(key):
        raise QueryError(f"Query key {key!r}: lowercase letters, digits and _")
    try:
        params = check_select(op.get("sql"))
    except QueryError as exc:
        raise QueryError(f"Query {key}: {exc}") from None
    return {"key": key, "label": str(op.get("label") or key)[:100], "method": "QUERY",
            "sql": op["sql"].strip().rstrip(";").rstrip(), "params": params}


class NamedTLSContext(ssl.SSLContext):
    """Verified TLS to a PINNED address (2026-11-08): the certificate is
    checked against the host NAME the tenant gave, whatever address the
    socket was opened to — so pinning the checked address (DNS rebinding)
    and verifying the certificate both hold. Used by the database query
    connector and HxDBMigrate sources."""
    velaris_name: str = ""

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        return super().wrap_bio(incoming, outgoing, server_side=server_side,
                                server_hostname=self.velaris_name or server_hostname, session=session)


def verified_tls(host_name: str) -> NamedTLSContext:
    """A verifying client context (system CAs) that checks `host_name`."""
    ctx = NamedTLSContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_default_certs()
    ctx.velaris_name = host_name
    return ctx
