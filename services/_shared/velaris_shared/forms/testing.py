"""Form test scenarios — sample answers with what should happen (Form Builder v2 §5 step 7, 2026-10-29).

A form may carry `tests: [{name, values, roles?, expect}]`:

* `values` — the answers, as a submission sends them;
* `roles`  — access roles to test as (field-level security); absent = unrestricted;
* `expect` — `{"valid": true|false, "errors": [field key, …], "values": {key: value}}`:
  accepted or refused; when refused, each named field has a problem (others
  may too); each named value is what the server stores (a calculation, a fill).

`POST /api/v1/forms/test-run` runs them through `validate`, the check every
submission meets, so a passing scenario is what production does.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
from typing import Any


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def judge(expect: Any, clean: dict[str, Any], errors: list[Any]) -> dict[str, Any]:
    """Did the check do what the scenario expects? `{passed, problems, errors, values}`."""
    expect = expect if isinstance(expect, dict) else {}
    problems: list[str] = []
    got = {e.field_key: e.message for e in errors}
    base = {k.split("[")[0] for k in got}
    valid = not errors
    if isinstance(expect.get("valid"), bool) and expect["valid"] != valid:
        problems.append("Expected it to be accepted" if expect["valid"] else "Expected it to be refused")
    for k in expect.get("errors") if isinstance(expect.get("errors"), list) else []:
        if str(k) not in got and str(k) not in base:
            problems.append(f"Expected a problem with {k}")
    for k, v in (expect.get("values") if isinstance(expect.get("values"), dict) else {}).items():
        if valid and not _same(clean.get(k), v):
            problems.append(f"Expected {k} = {json.dumps(v, default=str)}, got {json.dumps(clean.get(k), default=str)}")
    return {"passed": not problems, "problems": problems,
            "errors": [e.as_dict() for e in errors], "values": clean}
