"""Connector operations — several named requests on one connector (2026-11-04).

Until now a connector called one fixed address. An `http` or `http_custom`
connector may now list operations in its config:

    "operations": [
      {"key": "find_customer", "label": "Find customer", "method": "GET",
       "path": "/customers/{customer_id}"},
      {"key": "list_orders", "label": "List orders", "method": "GET", "path": "/orders",
       "response_mapping": {"orders": "data.items"}}
    ]

An operation's path is joined to the connector's OWN saved base address
(`base_url` for `http`, `url` for `http_custom`); a {placeholder} in it is
filled URL-encoded, so no input can change the host, add a query or climb a
directory. Who chooses the operation is the saved form field or step — never
the person filling the form. A connector without operations, or a call that
names none, works exactly as before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from typing import Any

OPERATION_TYPES = frozenset({"http", "http_custom", "sql_query"})
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
MAX_OPERATIONS = 50
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


#: Keys only this code sets on the config a connector is BUILT with — never a
#: saved value: a planted `_op_path` would add a path no operation names, a
#: planted `_platform_owned` would open the LAN to a tenant's connector.
RESERVED_KEYS = frozenset({"_op_path", "_op_path_placeholders", "_platform_owned", "_op_sql"})


class OperationError(ValueError):
    """A connector's operations are not well formed, or the one named is unknown."""


def _path_ok(path: Any) -> bool:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 500:
        return False
    if path.startswith("//") or "://" in path or "\\" in path:
        return False
    # No traversal, query or fragment written into the saved path either.
    segments = re.sub(r"\{\w+\}", "x", path).split("/")
    return not any(seg in (".", "..") for seg in segments) and "?" not in path and "#" not in path


def validate_operations(connector_type: str, config: dict | None) -> list[dict]:
    """The connector's operations, checked; raises OperationError when malformed.
    No `operations` key = an empty list (as before)."""
    planted = RESERVED_KEYS & set((config or {}).keys())
    if planted:
        raise OperationError(f"The config may not set {', '.join(sorted(planted))}")
    ops = (config or {}).get("operations")
    if ops is None:
        return []
    if connector_type not in OPERATION_TYPES:
        raise OperationError(f"A {connector_type} connector has no operations")
    if not isinstance(ops, list) or len(ops) > MAX_OPERATIONS:
        raise OperationError(f"operations must be a list of at most {MAX_OPERATIONS}")
    if connector_type == "sql_query":
        return _validate_queries(ops)
    seen: set[str] = set()
    out = []
    for op in ops:
        if not isinstance(op, dict):
            raise OperationError("Each operation is an object")
        key = op.get("key")
        if not isinstance(key, str) or not _KEY.match(key) or key in seen:
            raise OperationError(f"Operation key {key!r}: lowercase letters, digits and _, unique")
        method = str(op.get("method", "GET")).upper()
        if method not in METHODS:
            raise OperationError(f"Operation {key}: method must be one of {', '.join(sorted(METHODS))}")
        if not _path_ok(op.get("path")):
            raise OperationError(f"Operation {key}: path must start with / and name no host, "
                                 "query, fragment or ../")
        rm = op.get("response_mapping")
        if rm is not None and not (isinstance(rm, dict) and all(isinstance(v, str) for v in rm.values())):
            raise OperationError(f"Operation {key}: response_mapping maps names to paths")
        bt = op.get("body_template")
        if bt is not None and not isinstance(bt, str):
            raise OperationError(f"Operation {key}: body_template is text")
        seen.add(key)
        out.append({"key": key, "label": str(op.get("label") or key)[:100], "method": method,
                    "path": op["path"], **({"response_mapping": rm} if rm else {}),
                    **({"body_template": bt} if bt else {})})
    return out


def _validate_queries(ops: list) -> list[dict]:
    """A database query connector's operations are saved SELECTs (2026-11-08)."""
    from velaris_shared.hxbridge.sql_query import QueryError, check_query_operation
    seen: set[str] = set()
    out = []
    for op in ops:
        if not isinstance(op, dict):
            raise OperationError("Each query is an object")
        try:
            q = check_query_operation(op)
        except QueryError as exc:
            raise OperationError(str(exc)) from None
        if q["key"] in seen:
            raise OperationError(f"Query key {q['key']!r} is used twice")
        seen.add(q["key"])
        out.append(q)
    return out


def apply_operation(connector_type: str, config: dict | None, key: str | None) -> dict:
    """The config to run `key` with. None = the connector's own request, unchanged
    — minus any reserved key a saved config carries."""
    cfg = {k: v for k, v in (config or {}).items() if k not in RESERVED_KEYS}
    if not key:
        return cfg
    ops = {o["key"]: o for o in validate_operations(connector_type, cfg)}
    op = ops.get(key)
    if op is None:
        raise OperationError(f"This connector has no operation {key!r}")
    if connector_type == "sql_query":       # the saved SELECT, never the caller's
        cfg["_op_sql"] = op["sql"]
        return cfg
    cfg["method"] = op["method"]
    if connector_type == "http":
        cfg["path"] = op["path"]
        cfg["_op_path_placeholders"] = True
    else:                                   # http_custom: path joined to its url
        cfg["_op_path"] = op["path"]
        if "body_template" in op:
            cfg["body_template"] = op["body_template"]
        if "response_mapping" in op:
            cfg["response_mapping"] = op["response_mapping"]
    return cfg


def fill_path(path: str, data: dict) -> str:
    """Placeholders filled URL-encoded: a value cannot add `/`, `?`, `#` or `@`."""
    from urllib.parse import quote

    def rep(m: re.Match) -> str:
        val = data.get(m.group(1))
        return quote(str(val), safe="") if val is not None else ""
    return re.sub(r"\{(\w+)\}", rep, path)
