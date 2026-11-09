"""The server-side check of submitted form values (Form Builder redesign part 1, 2026-10-28).

Until now the server checked little — required by key presence (so `""` and
`null` passed), text length, number range, "contains @", dropdown membership —
and only on `POST /form-submissions/{id}`. Completing a step in Case Manager
checked nothing at all. Both paths now call `validate`.

The rules, in the order they apply to each field:

1. `hidden` / `read_only`: the person cannot change the value, through the UI or
   the API. The submitted value is dropped; `default_value`, when set, is used.
2. Visibility: `show_when` (or the legacy `conditions`) decides whether a field
   is shown. A field that is not shown is not required and is not submitted.
3. Required: `None`, `""` and `[]` are empty. `required_message` replaces the text.
4. Per type: text length, email, number range, option membership.

A definition without the new keys gets exactly the checks it had, except that
an empty value now fails `required`, and radio / multi-select values must be
among the options — which Studio never sent otherwise. Keys that are not form
fields are passed through untouched, as before.

Studio mirrors this in `studio/src/modules/form-builder/validate.ts`; both run
`validation_cases.json`.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import math
import pathlib
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Iterator

from velaris_shared.forms.expr import evaluate, references, truthy
from velaris_shared.forms.reference import matching_rows, ref_source, row_for

TEXT_TYPES = ("text", "textarea", "email", "phone", "url")
CHOICE_TYPES = ("dropdown", "radio")
#: A map pin (2026-10-29): `{"lat": …, "lng": …}`, stored as exactly that.
LOCATION = "location"

# ── Small controls (2026-10-29) ───────────────────────────────────────
#: Text, a line or a box the designer writes: they hold no value, and a value
#: sent under their key is dropped.
STATIC_TYPES = ("heading", "paragraph", "divider", "callout", "embed", "button")
_CODES = json.loads((pathlib.Path(__file__).parent / "codes.json").read_text())
COUNTRIES = frozenset(c["code"] for c in _CODES["countries"])
CURRENCIES = frozenset(c["code"] for c in _CODES["currencies"])
IBAN_LENGTHS: dict[str, int] = _CODES["iban_lengths"]
_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_URL = re.compile(r"https?://[^\s/?#.][^\s]*", re.IGNORECASE)
TAG_MAX_LENGTH = 50

# ── Controls batch M (2026-10-29) ─────────────────────────────────────
POSTCODE_PATTERNS: dict[str, str] = _CODES.get("postcode_patterns", {})
ADDRESS_PARTS = ("line1", "line2", "city", "region", "postcode", "country")
ADDRESS_PART_MAX = 200
#: A drawn signature: a PNG data URL, at most this many characters (~150 KB of image).
SIGNATURE_PREFIX = "data:image/png;base64,"
SIGNATURE_MAX = 200_000
RICH_TEXT_MAX = 10_000
#: A picker's value is an id; which ids are allowed is decided by the caller's
#: lookups (`velaris_shared.forms.pickers`) — None = not checked here.
PICKER_KINDS = {"user_picker": "user", "case_picker": "case", "document_picker": "document"}
_PICKER_WORD = {"user": "a person", "case": "a case", "document": "a document of this case"}
#: A ceiling whatever the designer sets: tags are typed by hand.
TAG_MAX_COUNT = 100

#: `validation.pattern` is a preset name or a regex of the designer's own.
#: "email" is how the Motor Insurance form names it (a text field).
PATTERN_PRESETS = {
    "email":     r"[^\s@]+@[^\s@]+\.[^\s@]{2,}",
    "digits":    r"[0-9]+",
    "alnum":     r"[A-Za-z0-9]+",
    "no_digits": r"[^0-9]+",
    "phone":     r"\+?[0-9 ()\-]{6,20}",
}
PRESET_MESSAGES = {
    "email": "Invalid email address", "digits": "Use digits only",
    "alnum": "Use letters and digits only", "no_digits": "Must not contain digits",
    "phone": "Invalid phone number",
}
#: A designer's regex runs on at most this many characters — the server never
#: hands an unbounded string to a pattern it did not write.
PATTERN_MAX_INPUT = 1000
#: …and only a pattern that cannot backtrack catastrophically (see `safe_pattern`).
PATTERN_MAX_LENGTH = 200
#: Show/hide rules that chain deeper than this are treated as shown, so a long
#: chain cannot exhaust the stack.
RULE_MAX_DEPTH = 64
#: A repeating group never holds more rows than this, whatever it allows.
ROWS_HARD_MAX = 500
GROUP = "repeating_group"
_RELATIVE = re.compile(r"^today(?:([+-])(\d{1,4}))?$")


@dataclass(frozen=True)
class FieldError:
    field_key: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _fields(definition: dict[str, Any] | None) -> Iterator[dict[str, Any]]:
    """Every field — a malformed definition yields what it can, never raises."""
    sections = definition.get("sections") if isinstance(definition, dict) else None
    for section in sections if isinstance(sections, list) else []:
        fields = section.get("fields") if isinstance(section, dict) else None
        for field in fields if isinstance(fields, list) else []:
            if isinstance(field, dict):
                yield field


def field_key_of(field: dict[str, Any]) -> str:
    """The key a field's value is stored under. `key` is HxMigrate's spelling:
    its forms carry neither `field_key` nor `id`, so every field of one shared
    the same empty key until 2026-10-28."""
    return str(field.get("field_key") or field.get("key") or field.get("id") or "")


_key = field_key_of


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _is_empty(val: Any) -> bool:
    return val is None or val == "" or (isinstance(val, list) and len(val) == 0)


def _s(val: Any) -> str:
    """One spelling for a value in a comparison — the same as Studio's."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _num(val: Any) -> float | None:
    if isinstance(val, bool):
        return float(val)
    try:
        n = float(val)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _fmt(n: Any) -> str:
    return _s(n)


def _equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        return any(_s(a) == _s(expected) for a in actual)
    return _s(actual) == _s(expected)


def _rule_holds(rule: dict[str, Any], actual: Any) -> bool:
    op = rule.get("op", "equals")
    expected = rule.get("value")
    if op == "equals":
        return _equals(actual, expected)
    if op == "not_equals":
        return not _equals(actual, expected)
    if op == "in":
        return any(_equals(actual, e) for e in (expected if isinstance(expected, list) else [expected]))
    if op == "is_empty":
        return _is_empty(actual)
    if op == "not_empty":
        return not _is_empty(actual)
    if op in ("gt", "lt"):
        a, e = _num(actual), _num(expected)
        if a is None or e is None or _is_empty(actual):
            return False
        return a > e if op == "gt" else a < e
    if op in ("before", "after"):
        a, e = _s(actual), _s(expected)
        if not a or not e:
            return False
        return a < e if op == "before" else a > e
    return False   # an operator this version does not know never shows a field by accident


def has_when(when: Any) -> bool:
    """Does this `show_when` / `required_when` / `read_only_when` say anything?"""
    if not isinstance(when, dict):
        return False
    if isinstance(when.get("expression"), str) and when["expression"].strip():
        return True
    return isinstance(when.get("rules"), list) and bool(when["rules"])


def _calc(field: dict[str, Any]) -> str | None:
    if field.get("type") == "consent":
        return None                # agreeing is never computed (2026-10-29)
    c = field.get("calculation")
    return c if isinstance(c, str) and c.strip() else None


class FormState:
    """What each field is, given these values: shown, locked (read-only), required,
    and its value — a calculated field's computed one (Form Builder v2, 2026-10-28).

    One resolver, so a condition, a calculation and a check all read the same
    thing: a field that is not shown reads as empty (a hidden *calculated* field
    still reads its value — it is the form's own arithmetic); a field the person
    cannot change reads its default. Anything that goes round in a circle (the
    builder warns about it) reads as before rules existed: shown, editable,
    optional, no value. Studio's `formState` in validate.ts is the twin.
    """

    def __init__(self, definition: dict[str, Any] | None, values: dict[str, Any], today: date,
                 context: dict[str, Any] | None = None, outer: Any = None,
                 references: dict[str, list[dict[str, Any]]] | None = None,
                 lookups: dict[str, set[str]] | None = None):
        self.order = list(_fields(definition))
        #: Ids a picker may hold, by kind (user / case / document); None = not checked.
        self.lookups = lookups
        #: Reference table rows by table id (`reference.load_rows`); None = not loaded (no check).
        self.references = references
        #: A field filled from a reference row: target key -> (source field, column).
        self.fill: dict[str, tuple[dict[str, Any], str]] = {}
        for f in self.order:
            src = ref_source(f)
            for m in (src.get("fill") if src and isinstance(src.get("fill"), list) else []):
                if isinstance(m, dict) and m.get("field") and m.get("column"):
                    self.fill.setdefault(str(m["field"]), (f, str(m["column"])))
        self.fields = {_key(f): f for f in self.order}
        self.by_id = {str(f.get("id")): _key(f) for f in self.fields.values() if f.get("id")}
        self.values, self.today, self.context = values, today, context
        #: In a repeating group's row: how a key that is not in the row reads (the form's).
        self.outer = outer
        #: A repeating group's row errors, by group key — filled as its rows are read.
        self.row_errors: dict[str, list[FieldError]] = {}
        self._memo: dict[tuple[str, str], Any] = {}
        self._busy: set[tuple[str, str]] = set()

    def _once(self, kind: str, key: str, depth: int, fallback: Any, compute: Any) -> Any:
        k = (kind, key)
        if k in self._memo:
            return self._memo[k]
        if depth > RULE_MAX_DEPTH or k in self._busy:
            return fallback
        self._busy.add(k)
        try:
            out = compute()
        finally:
            self._busy.discard(k)
        self._memo[k] = out
        return out

    def holds(self, when: Any, depth: int, *, legacy_rules: bool = True) -> bool:
        if isinstance(when.get("expression"), str) and when["expression"].strip():
            return truthy(evaluate(when["expression"], lambda k: self.reads(k, depth + 1, formula=True),
                                   today=self.today))
        results = [_rule_holds(r, self.reads(str(r.get("field", "")), depth + 1)) for r in when["rules"]
                   if isinstance(r, dict)]
        return any(results) if when.get("match") == "any" else all(results)

    def shown(self, key: str, depth: int = 0) -> bool:
        field = self.fields.get(key)
        if field is None:
            return True                       # not a form field: its value stands
        if field.get("hidden"):
            return False

        def compute() -> bool:
            when = field.get("show_when")
            if has_when(when):
                return self.holds(when, depth)
            if isinstance(field.get("conditions"), list) and field["conditions"]:
                # Legacy P38b shape: [{field_id, equals}], all must hold.
                return all(_equals(self.reads(str(self.by_id.get(str(c.get("field_id")), c.get("field_id", ""))), depth + 1),
                                   c.get("equals"))
                           for c in field["conditions"] if isinstance(c, dict))
            return True
        return self._once("shown", key, depth, True, compute)

    def locked(self, key: str, depth: int = 0) -> bool:
        """The person cannot change it: hidden, read-only, read-only now, or calculated."""
        field = self.fields.get(key)
        if field is None:
            return False
        if field.get("hidden") or field.get("read_only") or _calc(field):
            return True
        when = field.get("read_only_when")
        if not has_when(when):
            return False
        return self._once("locked", key, depth, False, lambda: self.holds(when, depth))

    def required(self, key: str, depth: int = 0) -> bool:
        field = self.fields.get(key)
        if field is None:
            return False
        if field.get("required"):
            return True
        when = field.get("required_when")
        if not has_when(when):
            return False
        return self._once("required", key, depth, False, lambda: self.holds(when, depth))

    def value(self, key: str, depth: int = 0) -> Any:
        """Its value, shown or not: computed, else the default when locked, else submitted."""
        field = self.fields.get(key)
        if field is None:
            return self.values.get(key)
        calc = _calc(field)
        if calc:
            return self._once("value", key, depth, None, lambda: evaluate(
                calc, lambda k: self.reads(k, depth + 1, formula=True), today=self.today))
        if field.get("type") == GROUP:
            return self._once("value", key, depth, None, lambda: self._rows(field, key, depth))
        if self.locked(key, depth) and key in self.fill and self.references is not None:
            # Filled from the chosen reference row: the table's value, never the one sent.
            return self._once("value", key, depth, None, lambda: self._filled(key, depth))
        if self.locked(key, depth):
            # The person cannot change it, so it holds what the case holds — the
            # value Studio pre-filled (`variable` binding, else the same name),
            # else its default. Without the case (`context` None) it reads the
            # submitted value, exactly as rules did before 2026-10-28.
            if self.context is not None:
                held = self.context.get(str(field.get("variable") or key))
                if held is not None:
                    return held
                return field.get("default_value")
        return self.values.get(key)

    def reads(self, key: str, depth: int, *, formula: bool = False) -> Any:
        """What a condition or a formula sees for `key`."""
        field = self.fields.get(key)
        if field is None:
            if self.outer is not None:            # a row: the rest of the form
                return self.outer(key)
            # A legacy rule may read a key that is not a field (as before); a
            # formula reads only the form — never a key the caller added.
            return None if formula else self.values.get(key)
        if field.get("hidden") and _calc(field):
            return self.value(key, depth)
        if not self.shown(key, depth):
            return None
        return self.value(key, depth)


    def ref_rows(self, field: dict[str, Any], depth: int = 0) -> list[dict[str, Any]] | None:
        """The reference rows this field may choose from now; None = not checked here."""
        src = ref_source(field)
        if src is None or self.references is None:
            return None
        return matching_rows(self.references.get(str(src.get("table_id") or ""), []), src,
                             lambda k: self.reads(k, depth + 1, formula=True))

    def _filled(self, key: str, depth: int) -> Any:
        source, column = self.fill[key]
        rows = self.ref_rows(source, depth)
        chosen = self.value(_key(source), depth + 1)
        if rows is None or chosen is None or isinstance(chosen, list):
            return None
        row = row_for(rows, ref_source(source) or {}, chosen)
        return row.get(column) if row else None

    def _rows(self, field: dict[str, Any], key: str, depth: int) -> list[dict[str, Any]] | None:
        """A repeating group's rows, each checked as a small form of its own
        (Form Builder v2): its formulas see the row first, then the form. The
        rows that come back hold only the group's own fields, with every
        calculated value the server's. Errors go to `row_errors[key]`."""
        children = group_fields(field)
        errors: list[FieldError] = []
        self.row_errors[key] = errors
        raw = self.values.get(key)
        if self.locked(key, depth):
            raw = (self.context or {}).get(str(field.get("variable") or key), field.get("default_value"))
        if raw is None:
            return None
        if not isinstance(raw, list):
            errors.append(FieldError(key, "Must be a list of rows"))
            return None
        if len(raw) > ROWS_HARD_MAX:
            errors.append(FieldError(key, f"At most {ROWS_HARD_MAX} rows"))
            return None
        held = (self.context or {}).get(str(field.get("variable") or key))
        held = held if isinstance(held, list) else []
        sub = {"sections": [{"fields": children}]}
        keys = {_key(c) for c in children}
        rows: list[dict[str, Any]] = []
        for i, row in enumerate(raw):
            if not isinstance(row, dict):
                errors.append(FieldError(f"{key}[{i}]", f"Row {i + 1} is not valid"))
                continue
            ctx = held[i] if i < len(held) and isinstance(held[i], dict) else {}
            rst = FormState(sub, dict(row), self.today, ctx,
                            outer=lambda k, d=depth: self.reads(k, d + 1, formula=True),
                            references=self.references, lookups=self.lookups)
            clean, errs = _check(rst, dict(row), self.today)
            rows.append({k: v for k, v in clean.items() if k in keys})
            errors.extend(FieldError(f"{key}[{i}].{e.field_key}", e.message) for e in errs)
        return rows


def group_fields(field: dict[str, Any]) -> list[dict[str, Any]]:
    """A repeating group's own fields — never another group (no nesting)."""
    raw = field.get("fields") if isinstance(field.get("fields"), list) else []
    return [c for c in raw if isinstance(c, dict) and c.get("type") != GROUP]


def visible_fields(definition: dict[str, Any] | None, values: dict[str, Any],
                   *, today: date | None = None) -> dict[str, bool]:
    """Field key -> is it shown, given these values."""
    st = FormState(definition, values, today or date.today())
    return {key: st.shown(key) for key in st.fields}


def _decimals(val: Any) -> int:
    text = _s(val)
    if "e" in text.lower():
        text = f"{float(text):f}".rstrip("0")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _bound(spec: Any, today: date) -> str | None:
    """A date bound: a fixed `YYYY-MM-DD`, or `today`, `today+N`, `today-N`."""
    if not isinstance(spec, str) or not spec:
        return None
    m = _RELATIVE.match(spec.strip())
    if m:
        n = int(m.group(2) or 0)
        return (today + timedelta(days=n if m.group(1) != "-" else -n)).isoformat()
    return spec[:10]


def safe_pattern(regex: str) -> bool:
    """Can this pattern run in time proportional to its input?

    Python's `re` has no time limit, and a designer's `(a+)+$` on 1000
    characters stops a worker for minutes — on every portal submit. Refused:
    a quantified group that itself repeats or alternates (`(a+)+`, `(a|ab)*`),
    a backreference, and a pattern longer than PATTERN_MAX_LENGTH. Studio
    applies the same rule (`validate.ts`), and the builder says so.
    """
    if len(regex) > PATTERN_MAX_LENGTH or re.search(r"\\[1-9]|\\k<", regex):
        return False
    stack: list[list[bool]] = []          # per open group: [has_quantifier, has_alternation]
    i, n, in_class = 0, len(regex), False
    while i < n:
        c = regex[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "(":
            stack.append([False, False])
        elif c == "|" and stack:
            stack[-1][1] = True
        elif c in "*+?{" and stack and (c != "?" or (i and regex[i - 1] not in "*+?}(")):
            stack[-1][0] = True
        elif c == ")" and stack:
            quant, alt = stack.pop()
            nxt = regex[i + 1] if i + 1 < n else ""
            if nxt and nxt in "*+{" and (quant or alt):
                return False
            if stack and (quant or (nxt and nxt in "*+?{")):
                stack[-1][0] = True
        i += 1
    return True


def _pattern_error(v: dict[str, Any], val: str) -> str | None:
    pattern = v.get("pattern")
    if not pattern or not isinstance(pattern, str):
        return None
    regex = PATTERN_PRESETS.get(pattern, pattern)
    if pattern not in PATTERN_PRESETS and not safe_pattern(regex):
        return None          # refused in the builder; never run
    message = v.get("pattern_message") or PRESET_MESSAGES.get(pattern, "Not in the expected format")
    if len(val) > PATTERN_MAX_INPUT:
        return message
    try:
        return None if re.fullmatch(regex, val) else message
    except re.error:
        return None          # a broken pattern is the builder's warning, never the person's error


def _file_error(v: dict[str, Any], val: Any) -> str | None:
    files = val if isinstance(val, list) else [val]
    if isinstance(val, list) and not v.get("multiple"):
        return "Only one file is allowed"
    max_files = v.get("max_files")
    if v.get("multiple") and _num(max_files) is not None and len(files) > _num(max_files):
        return f"At most {_fmt(max_files)} files"
    accept = [a.lower() if a.startswith(".") else f".{a.lower()}"
              for a in (v.get("accept") or []) if isinstance(a, str) and a]
    if accept:
        for f in files:
            name = (f.get("filename") if isinstance(f, dict) else "") or ""
            if not any(name.lower().endswith(a) for a in accept):
                return f"Allowed file types: {', '.join(accept)}"
    return None


def _coord(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
        return None
    return float(val)


def _location_error(val: Any) -> str | None:
    """A pin is two numbers, latitude -90..90 and longitude -180..180."""
    if not isinstance(val, dict):
        return "Must be a location"
    lat, lng = _coord(val.get("lat")), _coord(val.get("lng"))
    if lat is None or lng is None:
        return "Enter both latitude and longitude"
    if not -90 <= lat <= 90:
        return "Latitude must be between -90 and 90"
    if not -180 <= lng <= 180:
        return "Longitude must be between -180 and 180"
    return None


def _day(val: Any) -> date | None:
    if not isinstance(val, str) or not _DAY.fullmatch(val):
        return None
    try:
        return date.fromisoformat(val)
    except ValueError:
        return None


def _date_range_error(v: dict[str, Any], val: Any, today: date) -> str | None:
    """`{start, end}`, both days, end on or after start, inside the field's bounds."""
    start, end = (_day(val.get("start")), _day(val.get("end"))) if isinstance(val, dict) else (None, None)
    if start is None or end is None:
        return "Enter a start and an end date"
    if end < start:
        return "The end must be on or after the start"
    lo = today.isoformat() if v.get("no_past") else _bound(v.get("earliest"), today)
    hi = today.isoformat() if v.get("no_future") else _bound(v.get("latest"), today)
    if lo and start.isoformat() < lo:
        return f"Must be on or after {lo}"
    if hi and end.isoformat() > hi:
        return f"Must be on or before {hi}"
    most = _num(v.get("max_days"))
    if most is not None and (end - start).days + 1 > most:
        return f"At most {_fmt(v['max_days'])} days"
    return None


def iban_compact(val: str) -> str:
    return re.sub(r"\s+", "", val).upper()


def _iban_error(val: Any) -> str | None:
    """Country length and the ISO 13616 mod-97 check digits."""
    if not isinstance(val, str):
        return "Not a valid IBAN"
    iban = iban_compact(val)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", iban):
        return "Not a valid IBAN"
    want = IBAN_LENGTHS.get(iban[:2])
    if want is None:
        return f"{iban[:2]} does not use IBANs"
    if len(iban) != want:
        return f"An IBAN from {iban[:2]} has {want} characters"
    digits = "".join(str(int(ch, 36)) for ch in iban[4:] + iban[:4])
    return None if int(digits) % 97 == 1 else "Not a valid IBAN (check digits)"


def _tags_error(v: dict[str, Any], val: Any) -> str | None:
    if not isinstance(val, list) or any(not isinstance(t, str) or not t.strip() for t in val):
        return "Must be a list of tags"
    most = _num(v.get("max_tags"))
    most = TAG_MAX_COUNT if most is None else min(most, TAG_MAX_COUNT)
    if len(val) > most:
        return f"At most {_fmt(most)} tags"
    longest = _num(v.get("max_tag_length")) or TAG_MAX_LENGTH
    if any(len(t.strip()) > longest for t in val):
        return f"A tag has at most {_fmt(longest)} characters"
    if len({t.strip().lower() for t in val}) != len(val):
        return "Each tag only once"
    return None


def _address_error(val: Any) -> str | None:
    if not isinstance(val, dict):
        return "Must be an address"
    parts = {k: val.get(k) for k in ADDRESS_PARTS}
    if any(v is not None and not isinstance(v, str) for v in parts.values()):
        return "Must be an address"
    if any(len(v) > ADDRESS_PART_MAX for v in parts.values() if isinstance(v, str)):
        return f"Each part has at most {ADDRESS_PART_MAX} characters"
    if not (parts["line1"] or "").strip() or not (parts["city"] or "").strip():
        return "Enter the street and the town"
    country = (parts["country"] or "").strip().upper()
    if country not in COUNTRIES:
        return "Must be a country"
    postcode = (parts["postcode"] or "").strip()
    if postcode:
        pattern = POSTCODE_PATTERNS.get(country)
        ok = re.fullmatch(pattern, postcode, re.IGNORECASE) if pattern \
            else re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{1,11}", postcode)
        if not ok:
            return f"Not a postcode used in {country}"
    return None


def _signature_error(val: Any) -> str | None:
    import base64
    import binascii
    if not isinstance(val, str) or not val.startswith(SIGNATURE_PREFIX) or len(val) > SIGNATURE_MAX:
        return "Not a valid signature"
    try:
        head = base64.b64decode(val[len(SIGNATURE_PREFIX):], validate=True)[:8]
    except (binascii.Error, ValueError):
        return "Not a valid signature"
    return None if head == b"\x89PNG\r\n\x1a\n" else "Not a valid signature"


def phone_config(field: dict[str, Any]) -> dict[str, Any] | None:
    """A phone field's country settings (2026-11-04): `{default_country,
    countries}`. None — the field has none, or no library — keeps the format
    check every phone field had before."""
    if field.get("type") != "phone":
        return None
    cfg = field.get("phone")
    if not isinstance(cfg, dict) or cfg.get("default_country") not in COUNTRIES:
        return None
    try:
        import phonenumbers
    except ImportError:
        return None
    # A country with no dialling data (Antarctica…) cannot parse a number:
    # the plain format check, as with no country — Studio does the same.
    if cfg["default_country"] not in phonenumbers.SUPPORTED_REGIONS:
        return None
    return cfg


def parse_phone(cfg: dict[str, Any], val: str) -> tuple[str | None, str | None]:
    """(E.164, None) for a valid number with an allowed dialling code, else (None, why).
    A number written with its own +code is read as that country's; anything
    else as the field's default country's."""
    import phonenumbers
    from phonenumbers import NumberParseException, PhoneNumberFormat
    if len(val) > 40:
        return None, "Invalid phone number"
    try:
        num = phonenumbers.parse(val, cfg["default_country"])
    except NumberParseException:
        return None, "Invalid phone number"
    if not phonenumbers.is_valid_number(num):
        return None, "Not a valid phone number"
    # Allowed countries are matched by DIALLING CODE (GB allows every +44
    # number, Guernsey's too): the browser's library and this one disagree on
    # which of the regions sharing a code a number belongs to, never on its code.
    allowed = [c for c in cfg.get("countries") or []
               if isinstance(c, str) and c in COUNTRIES and c in phonenumbers.SUPPORTED_REGIONS]
    codes = {phonenumbers.country_code_for_region(c) for c in allowed}
    if allowed and num.country_code not in codes:
        return None, f"Use a number from {', '.join(allowed)}"
    return phonenumbers.format_number(num, PhoneNumberFormat.E164), None


def _type_error(field: dict[str, Any], val: Any, today: date) -> str | None:
    ftype = field.get("type", "text")
    v = field.get("validation") if isinstance(field.get("validation"), dict) else {}

    if ftype == "phone" and isinstance(val, str) and (cfg := phone_config(field)) is not None:
        return parse_phone(cfg, val)[1]

    if ftype in TEXT_TYPES and isinstance(val, str):
        if ftype == "url" and not _URL.fullmatch(val):
            return "Must be a web address (https://…)"
        if ftype == "email":
            if "@" not in val:
                return "Invalid email address"
            if v.get("format") == "strict" and not re.fullmatch(PATTERN_PRESETS["email"], val):
                return "Invalid email address"
            domains = [d.lower().lstrip("@") for d in v.get("allowed_domains") or [] if isinstance(d, str) and d]
            if domains:
                host = val.rsplit("@", 1)[1].lower()
                if not any(host == d or host.endswith("." + d) for d in domains):
                    return f"Use an address at {', '.join(domains)}"
        n = len(val)
        lo_len, hi_len = _num(v.get("min_length")), _num(v.get("max_length"))
        if lo_len and n < lo_len:
            return f"Must be at least {_fmt(v['min_length'])} chars"
        if hi_len and n > hi_len:
            return f"Must be at most {_fmt(v['max_length'])} chars"
        msg = _pattern_error(v, val)
        if msg:
            return msg

    if ftype in ("number", "currency", "slider", "rating", "percentage"):
        num = _num(val)
        if num is None:
            return "Must be a number"
        if ftype == "percentage":       # 0–100 unless the designer says otherwise
            v = {"min": 0, "max": 100, **{k: x for k, x in v.items() if x is not None}}
        lo = _num(v.get("min"))
        hi = _num(v.get("max_stars") if ftype == "rating" else v.get("max"))
        if lo is not None and num < lo:
            return f"Must be >= {_fmt(v['min'])}"
        if hi is not None and num > hi:
            return f"Must be <= {_fmt(v.get('max_stars') if ftype == 'rating' else v['max'])}"
        dec = _num(v.get("decimals"))
        if dec is not None and _decimals(val) > dec:
            return "Use whole numbers" if dec == 0 else f"Use at most {_fmt(dec)} decimals"

    if ftype in ("date", "datetime") and isinstance(val, str):
        day = val[:10]
        lo = today.isoformat() if v.get("no_past") else _bound(v.get("earliest"), today)
        hi = today.isoformat() if v.get("no_future") else _bound(v.get("latest"), today)
        if lo and day < lo:
            return f"Must be on or after {lo}"
        if hi and day > hi:
            return f"Must be on or before {hi}"

    if ftype == "file_upload":
        msg = _file_error(v, val)
        if msg:
            return msg

    if ftype == LOCATION:
        return _location_error(val)

    if ftype in ("toggle", "consent") and not isinstance(val, bool):
        return "Must be yes or no"

    if ftype == "time":
        if not isinstance(val, str) or not _TIME.fullmatch(val):
            return "Must be a time (HH:MM)"
        lo, hi = v.get("earliest"), v.get("latest")
        if isinstance(lo, str) and _TIME.fullmatch(lo) and val < lo:
            return f"Must be at or after {lo}"
        if isinstance(hi, str) and _TIME.fullmatch(hi) and val > hi:
            return f"Must be at or before {hi}"

    if ftype == "date_range":
        return _date_range_error(v, val, today)

    if ftype == "tags":
        return _tags_error(v, val)

    if ftype == "country" and val not in COUNTRIES:
        return "Must be a country"

    if ftype == "currency_code" and val not in CURRENCIES:
        return "Must be a currency"

    if ftype == "iban":
        return _iban_error(val)

    if ftype == "address":
        return _address_error(val)

    if ftype == "signature":
        return _signature_error(val)

    if ftype in PICKER_KINDS and (not isinstance(val, str) or not val.strip() or len(val) > 255):
        return "Choose from the list"

    if ftype == "rich_text":
        if not isinstance(val, str):
            return "Must be text"
        most = _num(v.get("max_length")) or RICH_TEXT_MAX
        if len(val) > min(most, RICH_TEXT_MAX):
            return f"Must be at most {_fmt(min(most, RICH_TEXT_MAX))} chars"

    if ftype in CHOICE_TYPES or ftype == "multi_select":
        chosen = val if ftype == "multi_select" else [val]
        if not isinstance(chosen, list):
            return "Must be a list of options"
        # Options a connector supplies at run time are not in the definition;
        # the server cannot check membership without calling it again.
        src = field.get("options_source")
        dynamic = isinstance(src, dict) and src.get("kind") == "connector"
        raw_options = field.get("options") if isinstance(field.get("options"), list) else []
        options = [o.get("value") if isinstance(o, dict) else o for o in raw_options]
        if options and not dynamic and any(c not in options for c in chosen):
            return "Must be one of the options"
        if ftype == "multi_select":
            lo, hi = _num(v.get("min_selected")), _num(v.get("max_selected"))
            if lo is not None and len(chosen) < lo:
                return f"Choose at least {_fmt(v['min_selected'])}"
            if hi is not None and len(chosen) > hi:
                return f"Choose at most {_fmt(v['max_selected'])}"
    return None


def validate(
    definition: dict[str, Any] | None, values: dict[str, Any] | None,
    *, today: date | None = None, context: dict[str, Any] | None = None,
    references: dict[str, list[dict[str, Any]]] | None = None,
    lookups: dict[str, set[str]] | None = None,
) -> tuple[dict[str, Any], list[FieldError]]:
    """(the values to store, the errors). No errors = accept.

    `today` anchors relative date bounds (`today+7`); the server's UTC date
    unless given. Studio uses the person's local date, so on the day boundary
    the two can differ by one day — the server decides.
    """
    today = today or date.today()
    values = dict(values or {})
    return _check(FormState(definition, values, today, context, references=references, lookups=lookups),
                  values, today)


def _check(st: FormState, values: dict[str, Any], today: date) -> tuple[dict[str, Any], list[FieldError]]:
    """`validate` for one form — or one row of a repeating group (its own FormState)."""
    clean = dict(values)
    errors: list[FieldError] = []
    for field in st.order:
        key = _key(field)
        if field.get("type") in STATIC_TYPES:
            clean.pop(key, None)
            continue
        if field.get("type") == "consent" and st.locked(key):
            # Agreeing is the person's own act: a hidden or read-only consent
            # never takes a designer's default, so it is simply not given.
            clean.pop(key, None)
            continue
        calculated = _calc(field) is not None
        group = field.get("type") == GROUP
        if field.get("hidden") and not calculated and not group:
            clean.pop(key, None)
            if field.get("default_value") is not None:
                clean[key] = field["default_value"]
            continue
        if not field.get("hidden") and not st.shown(key):
            clean.pop(key, None)
            continue
        if group:
            rows = st.value(key)
            clean.pop(key, None)
            if rows is not None:
                clean[key] = rows
            row_errors = st.row_errors.get(key, [])
            if any(e.field_key == key for e in row_errors):
                errors.extend(row_errors)
                continue
            n = len(rows or [])
            lo = _num(field.get("min_rows"))
            hi = _num(field.get("max_rows"))
            label = field.get("label") or key
            if n == 0 and st.required(key):
                errors.append(FieldError(key, str(field.get("required_message") or f"{label} is required")))
            elif lo is not None and n < lo:
                errors.append(FieldError(key, f"Add at least {_fmt(field['min_rows'])} rows"))
            elif hi is not None and n > hi:
                errors.append(FieldError(key, f"At most {_fmt(field['max_rows'])} rows"))
            else:
                msg = _check_error(field, st) if n else None
                if msg:
                    errors.append(FieldError(key, msg))
            errors.extend(row_errors)
            continue
        if calculated:
            # The server's arithmetic, never the browser's (2026-10-28).
            clean.pop(key, None)
            val = st.value(key)
            if val is not None:
                clean[key] = val
            if field.get("hidden"):
                continue
        elif st.locked(key):
            clean.pop(key, None)
            if key in st.fill and st.references is not None:
                filled = st.value(key)            # from the chosen reference row
                if filled is not None:
                    clean[key] = filled
            elif field.get("default_value") is not None:
                clean[key] = field["default_value"]
            continue
        else:
            val = values.get(key)
        if field.get("type") == "checkbox" and _dict(field.get("validation")).get("must_be_ticked") \
                and val is not True:
            errors.append(FieldError(key, str(field.get("required_message") or "Must be ticked")))
            continue
        if field.get("type") == "consent" and st.required(key) and val is not True:
            errors.append(FieldError(key, str(field.get("required_message") or "Please agree to continue")))
            continue
        if _is_empty(val):
            if st.required(key):
                errors.append(FieldError(key, str(field.get("required_message")
                                         or f"{field.get('label') or key} is required")))
            continue
        msg = _type_error(field, val, today) or _reference_error(field, val, st) or _picker_error(field, val, st)
        if msg:
            errors.append(FieldError(key, msg))
            continue
        msg = _check_error(field, st)
        if msg:
            errors.append(FieldError(key, msg))
            continue
        clean[key] = _stored(field.get("type"), val, field)

    return clean, errors


def _picker_error(field: dict[str, Any], val: Any, st: FormState) -> str | None:
    """A picker's id must be one the caller may choose: the server's lookups
    hold exactly those (people of the tenant, cases the caller can see,
    documents of this case). Not loaded = not checked, as reference rows."""
    kind = PICKER_KINDS.get(field.get("type"))
    if kind is None or st.lookups is None:
        return None
    return None if val in st.lookups.get(kind, set()) else f"Must be {_PICKER_WORD[kind]} you can choose"


def _stored(ftype: Any, val: Any, field: dict[str, Any] | None = None) -> Any:
    """The value as it is kept: a pin is its two numbers, a range its two
    days, tags trimmed, an IBAN without spaces, a country-aware phone number
    in international form (+447911123456) — nothing else rides along."""
    if ftype == "phone" and field is not None and isinstance(val, str):
        cfg = phone_config(field)
        if cfg is not None:
            e164, _err = parse_phone(cfg, val)
            return e164 or val
    if ftype == LOCATION:
        return {"lat": float(val["lat"]), "lng": float(val["lng"])}
    if ftype == "date_range":
        return {"start": val["start"], "end": val["end"]}
    if ftype == "tags":
        return [t.strip() for t in val]
    if ftype == "iban":
        return iban_compact(val)
    if ftype == "address":
        out = {k: val[k].strip() for k in ADDRESS_PARTS if isinstance(val.get(k), str) and val[k].strip()}
        out["country"] = out["country"].upper()
        return out
    return val


def _reference_error(field: dict[str, Any], val: Any, st: FormState) -> str | None:
    """A value chosen from a reference table must be one of its rows the field
    may choose from now (its cascading filters applied) — Form Builder v2."""
    rows = st.ref_rows(field)
    if rows is None:
        return None
    src = ref_source(field) or {}
    chosen = val if isinstance(val, list) else [val]
    if any(row_for(rows, src, c) is None for c in chosen):
        return "Must be one of the options"
    return None


def _check_error(field: dict[str, Any], st: FormState) -> str | None:
    """The first cross-field check that fails: `checks: [{expression, message}]`.
    A check waits until every field it names has a value — "end after start"
    says nothing while start is empty — and one that cannot be computed passes."""
    checks = field.get("checks")
    if not isinstance(checks, list):
        return None
    for c in checks:
        if not isinstance(c, dict) or not isinstance(c.get("expression"), str) or not c["expression"].strip():
            continue
        if any(_is_empty(st.reads(k, 1, formula=True)) for k in references(c["expression"])):
            continue
        r = evaluate(c["expression"], lambda k: st.reads(k, 1, formula=True), today=st.today)
        if r is not None and not truthy(r):
            return str(c.get("message") or "This value does not pass a check")
    return None


def form_keys(definition: dict[str, Any] | None) -> set[str]:
    """Every field key of the form — the only keys a submission stores (2026-10-28)."""
    return {_key(f) for f in _fields(definition)}

