"""Test Suite (#27) — the closed test DSL: types, parser, interpolation, asserts.

CLOSED by design (decision D4): a fixed action set, `{{var}}` interpolation, and
structured assertions only — never `eval`. This is what lets publisher-supplied
app-bundled tests (Phase F) run safely in a tenant environment.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── Closed action set ─────────────────────────────────────────────────────────
# Generic HTTP + a few high-level conveniences that expand to HTTP calls.
HTTP_ACTIONS = frozenset({"api_get", "api_post", "api_patch", "api_delete"})
HIGH_LEVEL_ACTIONS = frozenset({
    "create_case", "submit_via_portal", "stage_transition", "resolve_case",
    "time_travel", "assert_notification_sent",
})
ACTIONS = HTTP_ACTIONS | HIGH_LEVEL_ACTIONS

# Identities the runner can mint (decision D5). A step may name one; default admin.
IDENTITIES = frozenset({"admin", "non_admin", "tenant_a", "tenant_b", "none"})

# Structured assertion operators — no code execution.
ASSERT_OPS = frozenset({"eq", "ne", "contains", "gt", "lt", "status", "truthy", "has", "len"})

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class DslError(ValueError):
    """Raised when a test definition is malformed or uses an unknown action."""


@dataclass
class Step:
    action: str
    endpoint: str | None = None
    body: dict | None = None
    identity: str = "admin"
    capture: str | None = None         # "var = response.path"
    asserts: list[dict] = field(default_factory=list)  # [{path, op, value}]
    params: dict = field(default_factory=dict)         # high-level action params (offset_hours, etc.)


@dataclass
class TestCase:
    id: str
    name: str
    steps: list[Step]
    teardown: list[Step] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    suite: str | None = None
    module: str | None = None
    rationale: str | None = None       # populated by AI generation (Phase E)


# ── Parsing / validation ──────────────────────────────────────────────────────

def _parse_asserts(raw: Any) -> list[dict]:
    """Accept either a list of {path,op,value} or the shorthand dict
    {"response.status": "new"} (op defaults to eq; "response_status" → status)."""
    out: list[dict] = []
    if raw is None:
        return out
    if isinstance(raw, dict):
        for path, value in raw.items():
            op = "status" if path in ("response_status", "status_code") else "eq"
            out.append({"path": path, "op": op, "value": value})
        return out
    if isinstance(raw, list):
        for a in raw:
            if not isinstance(a, dict) or "path" not in a:
                raise DslError(f"assert entry must have a 'path': {a!r}")
            op = a.get("op", "eq")
            if op not in ASSERT_OPS:
                raise DslError(f"unknown assert op '{op}' (allowed: {sorted(ASSERT_OPS)})")
            out.append({"path": a["path"], "op": op, "value": a.get("value")})
        return out
    raise DslError(f"'assert' must be a dict or list, got {type(raw).__name__}")


def _parse_step(raw: dict) -> Step:
    if not isinstance(raw, dict):
        raise DslError(f"step must be a mapping, got {type(raw).__name__}")
    action = raw.get("action")
    if action not in ACTIONS:
        raise DslError(f"unknown action '{action}' (allowed: {sorted(ACTIONS)})")
    identity = raw.get("identity", "admin")
    if identity not in IDENTITIES:
        raise DslError(f"unknown identity '{identity}' (allowed: {sorted(IDENTITIES)})")
    if action in HTTP_ACTIONS and not raw.get("endpoint"):
        raise DslError(f"action '{action}' requires an 'endpoint'")
    known = {"action", "endpoint", "body", "identity", "capture", "assert"}
    params = {k: v for k, v in raw.items() if k not in known}
    return Step(
        action=action,
        endpoint=raw.get("endpoint"),
        body=raw.get("body"),
        identity=identity,
        capture=raw.get("capture"),
        asserts=_parse_asserts(raw.get("assert")),
        params=params,
    )


def parse_test(raw: dict) -> TestCase:
    if not isinstance(raw, dict):
        raise DslError("test must be a mapping")
    if not raw.get("id"):
        raise DslError("test requires an 'id'")
    if "steps" not in raw or not isinstance(raw["steps"], list):
        raise DslError(f"test '{raw.get('id')}' requires a 'steps' list")
    return TestCase(
        id=str(raw["id"]),
        name=raw.get("name", raw["id"]),
        steps=[_parse_step(s) for s in raw["steps"]],
        teardown=[_parse_step(s) for s in raw.get("teardown", [])],
        tags=list(raw.get("tags", [])),
        suite=raw.get("suite"),
        module=raw.get("module"),
        rationale=raw.get("rationale"),
    )


def parse_suite(raw: list) -> list[TestCase]:
    """Parse + validate a list of test definitions. Raises DslError on any problem.

    Used both for built-in suites and as the validation gate for AI-generated
    tests (parse before save; reject unknown actions)."""
    if not isinstance(raw, list):
        raise DslError("a suite definition must be a list of tests")
    return [parse_test(t) for t in raw]


# ── Interpolation + assertions (no eval) ──────────────────────────────────────

def _lookup(path: str, ctx: dict) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def interpolate(obj: Any, ctx: dict) -> Any:
    """Replace {{var}} / {{a.b}} references inside strings, recursively."""
    if isinstance(obj, str):
        def repl(m: re.Match) -> str:
            val = _lookup(m.group(1), ctx)
            return "" if val is None else str(val)
        # whole-string single ref → preserve type (e.g. an int id)
        m = _VAR_RE.fullmatch(obj.strip())
        if m:
            val = _lookup(m.group(1), ctx)
            return val if val is not None else obj
        return _VAR_RE.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: interpolate(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [interpolate(v, ctx) for v in obj]
    return obj


def apply_capture(spec: str, response_json: Any, ctx: dict) -> None:
    """`capture: "case_id = response.id"` → ctx["case_id"] = response_json["id"]."""
    if "=" not in spec:
        raise DslError(f"capture must be 'var = path', got {spec!r}")
    var, _, path = spec.partition("=")
    var, path = var.strip(), path.strip()
    if path.startswith("response."):
        ctx[var] = _lookup(path[len("response."):], _as_dict(response_json))
    else:
        ctx[var] = _lookup(path, ctx)


def _as_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def check_assert(a: dict, status_code: int, response_json: Any) -> tuple[bool, str]:
    """Evaluate one structured assertion against a response. Returns (ok, detail)."""
    path, op, expected = a["path"], a["op"], a.get("value")
    if op == "status" or path in ("response_status", "status_code"):
        ok = int(status_code) == int(expected)
        return ok, f"status {status_code} {'==' if ok else '!='} {expected}"
    # path like "response.field" or "response.a.b"
    actual = _lookup(path[len("response."):], _as_dict(response_json)) if path.startswith("response.") else None
    if op == "eq":
        ok = actual == expected
    elif op == "ne":
        ok = actual != expected
    elif op == "contains":
        ok = expected in actual if actual is not None else False
    elif op == "truthy":
        ok = bool(actual)
    elif op == "gt":
        ok = actual is not None and actual > expected
    elif op == "lt":
        ok = actual is not None and actual < expected
    elif op == "len":
        ok = isinstance(actual, (list, str, dict)) and len(actual) == expected
    elif op == "has":
        # `expected` appears in a list at `path` — directly or as an item's name/id/key.
        items = actual if isinstance(actual, list) else []
        ok = any(
            it == expected or (isinstance(it, dict) and expected in (it.get("name"), it.get("id"), it.get("key")))
            for it in items
        )
    else:
        return False, f"unknown op {op}"
    return ok, f"{path}={actual!r} {op} {expected!r} → {ok}"
