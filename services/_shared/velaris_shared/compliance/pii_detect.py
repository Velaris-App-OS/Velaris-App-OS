"""HxPrivacy — automated PII discovery & classification (§8.10).

Scans form and data-model definitions to find fields that probably hold
personal data, so retention and residency policy can be driven by what is
actually collected rather than by what someone remembered to tag.

**This detects; it does not enforce.** A finding is a proposal with a
confidence and a reason, reviewed by a human before it changes anything. That
split is deliberate in both directions:

* a **false positive** silently over-restricts a harmless field, and nobody
  notices until a feature mysteriously stops working;
* a **false negative** leaves personal data untagged while the dashboard says
  the scan passed — the more dangerous of the two, which is why the detector
  reports low-confidence guesses rather than discarding them.

Matching is heuristic — field names and labels, plus the declared input type.
It cannot read values (it never sees them) and does not pretend to: confidence
is reported on every finding and nothing here returns certainty.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Categories (closed vocabulary) ────────────────────────────────
EMAIL = "email"
PHONE = "phone"
NAME = "name"
ADDRESS = "address"
NATIONAL_ID = "national_id"
FINANCIAL = "financial"
HEALTH = "health"
BIOMETRIC = "biometric"
DATE_OF_BIRTH = "date_of_birth"
IP_ADDRESS = "ip_address"

CATEGORIES = frozenset({
    EMAIL, PHONE, NAME, ADDRESS, NATIONAL_ID, FINANCIAL,
    HEALTH, BIOMETRIC, DATE_OF_BIRTH, IP_ADDRESS,
})

#: GDPR Article 9 special categories — these carry a higher bar (explicit
#: consent) and are flagged separately so a reviewer cannot treat them as
#: ordinary personal data.
SPECIAL_CATEGORY = frozenset({HEALTH, BIOMETRIC})

#: Word boundary that treats `_` and `-` as separators.
#:
#: `\b` is wrong here: underscore is a WORD character in regex, so `\bemail\b`
#: does not match `customer_email` — the commonest field-naming convention in
#: the product. Using it produced silent false negatives, which is the
#: dangerous direction: personal data left untagged while the report looks
#: clean.
_B = r"(?<![a-z0-9])"      # not preceded by an alphanumeric
_E = r"(?![a-z0-9])"       # not followed by an alphanumeric


def _rule(body: str) -> re.Pattern[str]:
    return re.compile(_B + "(?:" + body + ")" + _E, re.I)


#: (category, pattern, confidence). Ordered: the first match wins, so more
#: specific patterns are listed before broader ones — "date_of_birth" must not
#: be swallowed by a generic "date" rule, and "email" must beat "name" on
#: `contact_email_name`.
_RULES: list[tuple[str, re.Pattern[str], float]] = [
    (EMAIL,         _rule(r"e[-_ ]?mail"), 0.95),
    (DATE_OF_BIRTH, _rule(r"dob|date[-_ ]?of[-_ ]?birth|birth[-_ ]?date|birthday"), 0.95),
    (NATIONAL_ID,   _rule(r"ssn|social[-_ ]?security|national[-_ ]?id|passport|nino|tax[-_ ]?id|aadhaar"), 0.95),
    (BIOMETRIC,     _rule(r"biometric|fingerprint|face[-_ ]?scan|iris|voice[-_ ]?print"), 0.9),
    (HEALTH,        _rule(r"health|medical|diagnosis|nhs|patient|disability|prescription"), 0.85),
    (FINANCIAL,     _rule(r"iban|bic|swift|card[-_ ]?number|account[-_ ]?number|sort[-_ ]?code|salary|income"), 0.9),
    (IP_ADDRESS,    _rule(r"ip[-_ ]?address|ipaddr|client[-_ ]?ip"), 0.9),
    (PHONE,         _rule(r"phone|mobile|telephone|msisdn|contact[-_ ]?number"), 0.9),
    (ADDRESS,       _rule(r"address|postcode|post[-_ ]?code|zip|city|street"), 0.8),
    (NAME,          _rule(r"first[-_ ]?name|last[-_ ]?name|surname|full[-_ ]?name|given[-_ ]?name|forename"), 0.85),
]

#: A declared input type is stronger evidence than a name, because it was
#: chosen deliberately rather than inherited from a copy-paste.
_TYPE_HINTS: dict[str, tuple[str, float]] = {
    "email": (EMAIL, 0.98),
    "tel": (PHONE, 0.95),
    "phone": (PHONE, 0.95),
}

#: A bare "name" is as likely to be a case type's name or a product name as a
#: person's. Reported, but low enough that a reviewer sees it as a question.
_WEAK_NAME = _rule(r"name")


def classify_field(
    field_key: str, label: str = "", field_type: str = "",
) -> dict[str, Any] | None:
    """Classify one field. Returns None when nothing suggests personal data.

    Never raises: a scanner that dies on one odd field definition would leave
    the rest of the form unexamined, which is the failure mode that produces a
    clean-looking report over unscanned data.
    """
    try:
        haystack = f"{field_key or ''} {label or ''}"

        hint = _TYPE_HINTS.get((field_type or "").strip().lower())
        if hint is not None:
            category, confidence = hint
            return _finding(field_key, label, category, confidence,
                            f"declared input type {field_type!r}")

        for category, pattern, confidence in _RULES:
            match = pattern.search(haystack)
            if match:
                return _finding(field_key, label, category, confidence,
                                f"matched {match.group(0)!r}")

        if _WEAK_NAME.search(haystack):
            return _finding(
                field_key, label, NAME, 0.4,
                "contains 'name', but this is as often a case or product name "
                "as a person's — needs a human decision",
            )
        return None
    except Exception:                              # pragma: no cover — defensive
        logger.debug("classify_field failed for %r", field_key, exc_info=True)
        return None


def _finding(field_key, label, category, confidence, reason) -> dict[str, Any]:
    return {
        "field_key": field_key,
        "field_label": label or "",
        "category": category,
        "confidence": round(float(confidence), 2),
        "reason": reason,
        "special_category": category in SPECIAL_CATEGORY,
    }


def scan_form_definition(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Findings for a form. Shape is `sections[].fields[]`."""
    out: list[dict[str, Any]] = []
    for section in (definition or {}).get("sections", []) or []:
        for field in (section or {}).get("fields", []) or []:
            if not isinstance(field, dict):
                continue
            finding = classify_field(
                str(field.get("field_key") or field.get("id") or ""),
                str(field.get("label") or ""),
                str(field.get("type") or ""),
            )
            if finding:
                finding["field_path"] = (
                    f"{section.get('id', '?')}.{finding['field_key']}"
                )
                out.append(finding)
    return out


def scan_data_model(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Findings for a data model. Shape is `fields[]` with `name`/`field_type`."""
    out: list[dict[str, Any]] = []
    for field in (definition or {}).get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        finding = classify_field(
            str(field.get("name") or ""),
            str(field.get("label") or ""),
            str(field.get("field_type") or field.get("type") or ""),
        )
        if finding:
            finding["field_path"] = finding["field_key"]
            out.append(finding)
    return out
