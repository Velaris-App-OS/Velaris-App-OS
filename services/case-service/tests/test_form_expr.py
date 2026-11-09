"""The form expression language (Form Builder v2, 2026-10-28).

`velaris_shared.forms.expr` runs the case table Studio's `expr.ts` also runs
(`expression_cases.json`), so a formula means the same on both sides.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import pathlib
import random
from datetime import date

import pytest

import velaris_shared.forms as _forms_pkg
from velaris_shared.forms.expr import check, evaluate, references

TABLE = json.loads((pathlib.Path(_forms_pkg.__file__).parent / "expression_cases.json").read_text())
TODAY = date.fromisoformat(TABLE["today"])


@pytest.mark.parametrize("case", TABLE["cases"], ids=[c["name"] for c in TABLE["cases"]])
def test_shared_case_table(case):
    values = {**TABLE["values"], **case.get("values", {})}
    if case.get("error"):
        assert check(case["expr"]) is not None
        assert evaluate(case["expr"], values.get, today=TODAY) is None
        return
    assert check(case["expr"]) is None, check(case["expr"])
    assert evaluate(case["expr"], values.get, today=TODAY) == case["result"]


def test_references():
    assert references("qty * rec.amount + {not} + sum(a, b)") == {"qty", "rec", "not", "a", "b"}
    assert references("foo(") == set()


def test_never_raises_on_any_input():
    """Fuzz: random token soup and hostile values never raise and never hang."""
    rng = random.Random(7)
    atoms = ["qty", "1", "0", "-", "+", "*", "/", "%", "^", "&", "(", ")", ",", "=", "<", "and", "not",
             "if(", "sum(", "round(", "\"x\"", "{a}", "rec.city", "today()", "add_days(", "years_between("]
    values = {"qty": [1, {"a": None}], "rec": "text", "a": float("nan")}
    for _ in range(3000):
        src = " ".join(rng.choice(atoms) for _ in range(rng.randint(0, 25)))
        check(src)
        evaluate(src, values.get, today=TODAY)
    for bad in (None, 5, {"x": 1}, ["a"], "(" * 5000, "1" * 3000):
        assert check(bad) is not None
        assert evaluate(bad, values.get) is None
