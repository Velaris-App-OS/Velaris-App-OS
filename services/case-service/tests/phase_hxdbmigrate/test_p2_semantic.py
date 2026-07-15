"""HxDBMigrate P2 — semantic classification, compliance scan, type mapping (DB-free units)."""
from __future__ import annotations

import datetime

from case_service.hxdbmigrate import compliance, mapping
from case_service.hxdbmigrate import semantic as sem


# ── semantic classification ───────────────────────────────────────────────────

def test_classify_email():
    c = sem.classify_column("email", ["a@x.com", "b@y.org", "c@z.net"])
    assert c["category"] == "email" and c["confidence"] >= 0.9


def test_classify_ssn():
    assert sem.classify_column("ssn", ["123-45-6789", "987-65-4321"])["category"] == "ssn"


def test_classify_credit_card_requires_luhn():
    # 4111111111111111 is a valid Luhn test number; a random 16-digit string is not.
    assert sem.classify_column("card", ["4111 1111 1111 1111"] * 3)["category"] == "credit_card"
    assert sem.classify_column("card", ["1234 5678 9012 3456"] * 3)["category"] != "credit_card"


def test_classify_id_and_enum_and_numeric():
    assert sem.classify_column("user_id", [1, 2, 3])["category"] == "id"
    assert sem.classify_column("status", ["A", "B", "A", "B", "A", "B"])["category"] == "enum"
    assert sem.classify_column("amount", ["1.5", "2.0", "3.25"])["category"] == "numeric"


def test_classify_dob_needs_name_and_dates():
    vals = [datetime.date(1990, 1, 1), datetime.date(1985, 5, 5)]
    assert sem.classify_column("date_of_birth", vals)["category"] == "date_of_birth"


def test_masking_never_returns_raw():
    assert sem.mask("email", "alice@acme.com") == "a***@***"
    assert sem.mask("ssn", "123-45-6789").endswith("6789") and "123" not in sem.mask("ssn", "123-45-6789")
    assert sem.mask("credit_card", "4111111111111111") == "***1111"


def test_luhn():
    assert sem._luhn("4111111111111111") and not sem._luhn("4111111111111112")


# ── compliance scan ───────────────────────────────────────────────────────────

def test_compliance_scan_flags_and_actions():
    semantic_by_table = {
        "people": {
            "email": {"category": "email", "masked_examples": ["a***@***"]},
            "ssn": {"category": "ssn", "masked_examples": ["***6789"]},
            "card_number": {"category": "credit_card", "masked_examples": ["***1111"]},
            "full_name": {"category": "text"},          # caught by name hint
            "notes": {"category": "free_text"},          # not sensitive
        }
    }
    out = compliance.scan(semantic_by_table)
    by_col = {f["column"]: f for f in out["findings"]}
    assert set(by_col) == {"email", "ssn", "card_number", "full_name"}
    assert by_col["ssn"]["recommended_action"] == "tokenize"
    assert by_col["card_number"]["recommended_action"] == "tokenize"
    assert out["summary"]["pii_column_count"] == 4
    assert "people.ssn" in out["summary"]["tokenize_required"]


# ── type mapping ──────────────────────────────────────────────────────────────

def test_mapping_tinyint_warns_boolean():
    m = mapping.map_type("tinyint(1)")
    assert m["target_type"] == "smallint" and "boolean" in m["warning"].lower()


def test_mapping_enum_and_json_and_unknown():
    assert "lookup" in mapping.map_type("enum('a','b')")["warning"].lower()
    assert mapping.map_type("json")["target_type"] == "jsonb"
    assert mapping.map_type("weirdtype")["target_type"] == "text"
