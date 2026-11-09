"""The form expression language — calculations, conditions and checks (Form Builder v2, 2026-10-28).

One small language for every place a form computes something: a calculated
value (`quantity * unit_price`), a condition (`amount > 1000 and country = "DE"`)
and a cross-field check (`end_date >= start_date`). Spreadsheet-like on
purpose — the people who write these know Excel, not Python.

    numbers 12  3.5        text "abc" or 'abc' ("" inside doubles a quote)
    true  false  null      a field: its key (`unit_price`, `lookup.result.city`)
                           or any key in braces (`{first-name}`)
    + - * / % ^  (also × ÷)     & joins text
    = == != <> < <= > >=        and or not (also && || !)
    function(args) — see FUNCTIONS; names are not case-sensitive. A leading
    "=" is allowed and ignored.

It is evaluated by this interpreter and by its twin in Studio
(`studio/src/modules/form-builder/expr.ts`); both run `expression_cases.json`.
Never `eval`: the source is parsed into a tree, the tree has no loops and no
way to reach anything but the form's own values, and its size is bounded, so a
designer cannot hang a worker or read outside the form.

Evaluation never raises. A value it cannot compute is `null`: arithmetic with
an empty field, division by zero, a date that is not a date. A total stays
empty until its inputs are filled, which is what a person expects.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any, Callable

MAX_LENGTH = 2000       # characters of source
MAX_NODES = 500         # parsed tree size
MAX_DEPTH = 64          # nesting
MAX_TEXT = 10000        # characters of any text result
MAX_ROUND = 10          # decimals round() accepts

FUNCTIONS: dict[str, tuple[int, int | None]] = {
    # name: (min args, max args or None for any)
    "if": (2, 3), "coalesce": (1, None), "is_empty": (1, 1),
    "round": (1, 2), "floor": (1, 1), "ceil": (1, 1), "abs": (1, 1),
    "min": (1, None), "max": (1, None), "sum": (1, None), "avg": (1, None), "count": (1, None),
    "len": (1, 1), "upper": (1, 1), "lower": (1, 1), "trim": (1, 1), "concat": (1, None),
    "contains": (2, 2), "number": (1, 1), "text": (1, 1),
    "today": (0, 0), "days_between": (2, 2), "years_between": (2, 2), "add_days": (2, 2),
}

_NUMERIC = re.compile(r"^\s*-?(?:\d+(?:\.\d*)?|\.\d+)\s*$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_NUMBER = re.compile(r"(?:\d+(?:\.\d+)?|\.\d+)")
_WORDS = {"and", "or", "not", "true", "false", "null"}


class ExprError(ValueError):
    """The source is not a valid expression. `message` is for the designer."""


# ── parsing ──────────────────────────────────────────────────────────────────
# Nodes are tuples: ("lit", v) ("ref", key) ("call", name, [args]) ("not", a)
# ("neg", a) ("and", a, b) ("or", a, b) ("bin", op, a, b).

def _tokens(src: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c in "\"'":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ExprError("A piece of text is not closed — add the missing quote.")
                if src[j] == c:
                    if j + 1 < n and src[j + 1] == c:
                        buf.append(c)
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            out.append(("str", "".join(buf)))
            i = j + 1
            continue
        if c == "{":
            j = src.find("}", i + 1)
            if j < 0 or j == i + 1:
                raise ExprError("A field in { } is not closed or is empty.")
            out.append(("bref", src[i + 1:j].strip()))
            i = j + 1
            continue
        m = _NUMBER.match(src, i)
        if m and (c.isdigit() or c == "."):
            out.append(("num", float(m.group())))
            i = m.end()
            continue
        m = _IDENT.match(src, i)
        if m:
            word = m.group()
            low = word.lower()
            if low in _WORDS:
                out.append(("word", low))
            else:
                out.append(("name", word))
            i = m.end()
            continue
        two = src[i:i + 2]
        if two in ("==", "!=", "<>", "<=", ">=", "&&", "||"):
            out.append(("op", {"==": "=", "<>": "!=", "&&": "and", "||": "or"}.get(two, two)))
            i += 2
            continue
        if c in "+-*/%^&=<>!(),×÷":
            out.append(("op", {"×": "*", "÷": "/", "!": "not"}.get(c, c)))
            i += 1
            continue
        raise ExprError(f"“{c}” cannot be used here.")
    return out


class _Parser:
    def __init__(self, toks: list[tuple[str, Any]]):
        self.t, self.i, self.nodes, self.depth = toks, 0, 0, 0

    def peek(self) -> tuple[str, Any] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, kind: str, value: Any = None) -> bool:
        p = self.peek()
        if p and p[0] in (kind, "word" if kind == "op" else kind) and (value is None or p[1] == value):
            self.i += 1
            return True
        return False

    def node(self, *parts: Any) -> tuple:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ExprError("This formula is too long — split it over more fields.")
        return parts

    def nest(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExprError("This formula is nested too deeply.")

    def parse(self) -> tuple:
        if not self.t:
            raise ExprError("The formula is empty.")
        n = self.or_()
        if self.peek() is not None:
            raise ExprError(f"Unexpected “{self.peek()[1]}” — is an operator or a comma missing?")
        return n

    def or_(self) -> tuple:
        a = self.and_()
        while self.take("op", "or"):
            a = self.node("or", a, self.and_())
        return a

    def and_(self) -> tuple:
        a = self.not_()
        while self.take("op", "and"):
            a = self.node("and", a, self.not_())
        return a

    def not_(self) -> tuple:
        if self.take("op", "not"):
            self.nest()
            try:
                return self.node("not", self.not_())
            finally:
                self.depth -= 1
        return self.cmp()

    def cmp(self) -> tuple:
        a = self.add()
        p = self.peek()
        if p and p[0] == "op" and p[1] in ("=", "!=", "<", "<=", ">", ">="):
            self.i += 1
            a = self.node("bin", p[1], a, self.add())
        return a

    def add(self) -> tuple:
        a = self.mul()
        while True:
            p = self.peek()
            if p and p[0] == "op" and p[1] in ("+", "-", "&"):
                self.i += 1
                a = self.node("bin", p[1], a, self.mul())
            else:
                return a

    def mul(self) -> tuple:
        a = self.unary()
        while True:
            p = self.peek()
            if p and p[0] == "op" and p[1] in ("*", "/", "%"):
                self.i += 1
                a = self.node("bin", p[1], a, self.unary())
            else:
                return a

    def unary(self) -> tuple:
        if self.take("op", "-"):
            self.nest()
            try:
                return self.node("neg", self.unary())
            finally:
                self.depth -= 1
        if self.take("op", "+"):
            return self.unary()
        return self.power()

    def power(self) -> tuple:
        a = self.primary()
        if self.take("op", "^"):
            self.nest()
            try:
                return self.node("bin", "^", a, self.unary())
            finally:
                self.depth -= 1
        return a

    def primary(self) -> tuple:
        p = self.peek()
        if p is None:
            raise ExprError("The formula ends too early — something is missing at the end.")
        kind, v = p
        self.i += 1
        if kind == "num":
            return self.node("lit", v)
        if kind == "str":
            return self.node("lit", v)
        if kind == "word" and v in ("true", "false", "null"):
            return self.node("lit", {"true": True, "false": False, "null": None}[v])
        if kind == "bref":
            return self.node("ref", v, True)          # braces: the exact key, no path
        if kind == "name":
            if self.take("op", "("):
                name = v.lower()
                if name not in FUNCTIONS:
                    raise ExprError(f"There is no function called {v}.")
                args: list[tuple] = []
                self.nest()
                try:
                    if not self.take("op", ")"):
                        while True:
                            args.append(self.or_())
                            if self.take("op", ")"):
                                break
                            if not self.take("op", ","):
                                raise ExprError(f"{v}( is not closed — add “)”.")
                finally:
                    self.depth -= 1
                lo, hi = FUNCTIONS[name]
                if len(args) < lo or (hi is not None and len(args) > hi):
                    want = f"{lo}" if lo == hi else (f"at least {lo}" if hi is None else f"{lo} to {hi}")
                    raise ExprError(f"{name}() takes {want} value(s), not {len(args)}.")
                return self.node("call", name, args)
            return self.node("ref", v, False)
        if kind == "op" and v == "(":
            self.nest()
            try:
                n = self.or_()
            finally:
                self.depth -= 1
            if not self.take("op", ")"):
                raise ExprError("A bracket is not closed — add “)”.")
            return n
        raise ExprError(f"Unexpected “{v}”.")


def parse(src: Any) -> tuple:
    """The tree for `src`, or ExprError with a message for the designer."""
    if not isinstance(src, str):
        raise ExprError("A formula must be text.")
    return _parse(src)


@lru_cache(maxsize=1024)
def _parse(src: str) -> tuple:
    if len(src) > MAX_LENGTH:
        raise ExprError(f"This formula is longer than {MAX_LENGTH} characters.")
    s = src.strip()
    if s.startswith("="):
        s = s[1:]
    return _Parser(_tokens(s)).parse()


def check(src: Any) -> str | None:
    """None when `src` parses; else the message to show the designer."""
    try:
        parse(src)
        return None
    except ExprError as e:
        return str(e)
    except RecursionError:
        return "This formula is nested too deeply."


def references(src: Any) -> set[str]:
    """The field keys `src` reads (the first segment of a dotted path)."""
    out: set[str] = set()
    try:
        tree = parse(src)
    except (ExprError, RecursionError):
        return out

    def walk(n: Any) -> None:
        if not isinstance(n, tuple):
            return
        if n[0] == "ref":
            out.add(str(n[1]) if n[2] else str(n[1]).split(".")[0])
            return
        for part in n[1:]:
            if isinstance(part, tuple):
                walk(part)
            elif isinstance(part, list):
                for a in part:
                    walk(a)
    walk(tree)
    return out


# ── values ───────────────────────────────────────────────────────────────────

def is_empty(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, (list, dict)) and len(v) == 0)


def truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return not is_empty(v)


def to_num(v: Any) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, str) and _NUMERIC.match(v):
        return float(v)
    return None


def _num_text(x: float) -> str:
    """A number as JavaScript's String(x) writes it, so both sides agree."""
    if x == 0:
        return "0"
    if x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    r = repr(x)
    if 1e-6 <= abs(x) < 1e21:
        return format(Decimal(r), "f") if "e" in r else r
    mant, exp = r.split("e") if "e" in r else (r, "0")
    e = int(exp)
    return f"{mant}e{'+' if e >= 0 else '-'}{abs(e)}"


def to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        f = float(v)
        return _num_text(f) if math.isfinite(f) else ""
    if isinstance(v, list):
        return ", ".join(to_text(x) for x in v)
    if isinstance(v, dict):
        return ""
    return str(v)


def _out(v: Any) -> Any:
    """Results leave as plain JSON values: integral numbers as int, no NaN."""
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        if v.is_integer() and abs(v) < 2 ** 53:
            return int(v)
    if isinstance(v, str) and len(v) > MAX_TEXT:
        return None
    return v


def _date(v: Any) -> date | None:
    if not isinstance(v, str):
        return None
    m = _DATE.match(v.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _flat(args: list[Any]) -> list[Any]:
    out: list[Any] = []
    for a in args:
        if isinstance(a, list):
            out.extend(a)
        else:
            out.append(a)
    return out


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, list):
        return any(_equal(x, b) for x in a)
    if is_empty(a) or is_empty(b):
        return is_empty(a) and is_empty(b)
    na, nb = to_num(a), to_num(b)
    if na is not None and nb is not None and not isinstance(a, bool) and not isinstance(b, bool):
        return na == nb
    return to_text(a) == to_text(b)


def _order(op: str, a: Any, b: Any) -> bool:
    if is_empty(a) or is_empty(b) or isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return False
    na, nb = to_num(a), to_num(b)
    if na is not None and nb is not None:
        x, y = na, nb
    else:
        x, y = to_text(a), to_text(b)          # ISO dates compare as text
    return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]


def _arith(op: str, a: Any, b: Any) -> Any:
    x, y = to_num(a), to_num(b)
    if x is None or y is None or is_empty(a) or is_empty(b):
        return None
    try:
        if op == "+":
            r = x + y
        elif op == "-":
            r = x - y
        elif op == "*":
            r = x * y
        elif op == "/":
            if y == 0:
                return None
            r = x / y
        elif op == "%":
            if y == 0:
                return None
            r = x - y * math.floor(x / y)       # floored, as a spreadsheet's MOD
        else:                                   # "^"
            if x < 0 and not y.is_integer():
                return None
            if x == 0 and y < 0:
                return None
            r = x ** y
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    return r if isinstance(r, float) and math.isfinite(r) else (float(r) if isinstance(r, int) else None)


def _round(x: float, n: int) -> float:
    """Half away from zero, on the decimal a person sees: 8.925 is 892.4999… in
    binary, and a spreadsheet still rounds it to 8.93. 15 significant digits
    first — the same digits Studio's toPrecision(15) gives."""
    p = 10.0 ** n
    v = float(format(abs(x) * p, ".15g"))
    return math.copysign(math.floor(v + 0.5) / p, x)


def _call(name: str, args: list[Any], today: date) -> Any:
    if name == "coalesce":
        return next((a for a in args if not is_empty(a)), None)
    if name == "is_empty":
        return is_empty(args[0])
    if name in ("round", "floor", "ceil", "abs"):
        x = to_num(args[0])
        if x is None or is_empty(args[0]):
            return None
        if name == "round":
            n = to_num(args[1]) if len(args) > 1 else 0.0
            if n is None or not n.is_integer() or not 0 <= n <= MAX_ROUND:
                return None
            return _round(x, int(n))
        return float({"floor": math.floor, "ceil": math.ceil, "abs": abs}[name](x))
    if name in ("min", "max", "sum", "avg"):
        nums = [to_num(a) for a in _flat(args) if not is_empty(a)]
        nums = [n for n in nums if n is not None]
        total = 0.0
        for x in nums:                          # left to right, as Studio adds
            total += x
        if name == "sum":
            return total
        if not nums:
            return None
        if name == "avg":
            return total / len(nums)
        return min(nums) if name == "min" else max(nums)
    if name == "count":
        return float(sum(1 for a in _flat(args) if not is_empty(a)))
    if name == "len":
        return float(len(to_text(args[0])))
    if name in ("upper", "lower", "trim"):
        t = to_text(args[0])
        return t.upper() if name == "upper" else t.lower() if name == "lower" else t.strip()
    if name == "concat":
        return "".join(to_text(a) for a in args)
    if name == "contains":
        hay, needle = args
        if isinstance(hay, list):
            return any(_equal(x, needle) for x in hay)
        return to_text(needle) in to_text(hay) if not is_empty(needle) else False
    if name == "number":
        return to_num(args[0]) if not is_empty(args[0]) else None
    if name == "text":
        return to_text(args[0])
    if name == "today":
        return today.isoformat()
    if name in ("days_between", "years_between"):
        a, b = _date(args[0]), _date(args[1])
        if a is None or b is None:
            return None
        if name == "days_between":
            return float((b - a).days)
        years = b.year - a.year - (1 if (b.month, b.day) < (a.month, a.day) else 0)
        return float(years)
    if name == "add_days":
        d, n = _date(args[0]), to_num(args[1])
        if d is None or n is None or not n.is_integer() or abs(n) > 100000:
            return None
        try:
            return (d + timedelta(days=int(n))).isoformat()
        except OverflowError:
            return None
    return None


def _path(v: Any, rest: list[str]) -> Any:
    """Follow `rest` into `v`. Through a list (a repeating group's rows) it
    collects the value from every row: `items.price` is every row's price."""
    for i, k in enumerate(rest):
        if isinstance(v, list):
            return [_path(e, rest[i:]) for e in v]
        v = v.get(k) if isinstance(v, dict) else None
    return v


def evaluate(src: Any, lookup: Callable[[str], Any], *, today: date | None = None) -> Any:
    """The value of `src`, reading fields through `lookup(key)`. Never raises: an
    expression that does not parse, or a value it cannot compute, is None."""
    try:
        tree = parse(src) if not isinstance(src, tuple) else src
    except (ExprError, RecursionError):
        return None
    today = today or date.today()

    def ev(n: tuple) -> Any:
        kind = n[0]
        if kind == "lit":
            return n[1]
        if kind == "ref":
            key = str(n[1])
            if "." in key and not n[2]:
                head, *rest = key.split(".")
                whole = lookup(key)
                return whole if whole is not None else _path(lookup(head), rest)
            return lookup(key)
        if kind == "not":
            return not truthy(ev(n[1]))
        if kind == "neg":
            x = ev(n[1])
            y = to_num(x)
            return None if y is None or is_empty(x) else -y
        if kind == "and":
            return truthy(ev(n[1])) and truthy(ev(n[2]))
        if kind == "or":
            return truthy(ev(n[1])) or truthy(ev(n[2]))
        if kind == "call":
            name, args = n[1], n[2]
            if name == "if":
                return ev(args[1]) if truthy(ev(args[0])) else (ev(args[2]) if len(args) > 2 else None)
            return _call(name, [ev(a) for a in args], today)
        op, a, b = n[1], ev(n[2]), ev(n[3])
        if op == "=":
            return _equal(a, b)
        if op == "!=":
            return not _equal(a, b)
        if op in ("<", "<=", ">", ">="):
            return _order(op, a, b)
        if op == "&":
            t = to_text(a) + to_text(b)
            return t if len(t) <= MAX_TEXT else None
        return _arith(op, a, b)

    try:
        return _out(ev(tree))
    except (RecursionError, TypeError, ValueError, OverflowError):
        return None
