"""HxDBMigrate P3 — case-type generation helpers (DB-free units).

The live generate+apply path is exercised in the P3 live test; these pin the pure logic:
workflow detection, status-column selection, field-type mapping, and the identifier slug.
"""
from __future__ import annotations

from case_service.hxdbmigrate import casegen


def _col(name, type_="varchar", nullable="YES", category=None):
    c = {"name": name, "type": type_, "nullable": nullable}
    if category:
        c["semantic"] = {"category": category}
    return c


def test_snake():
    assert casegen._snake("Order Status") == "order_status"
    assert casegen._snake("customer-id!") == "customer_id"
    assert casegen._snake("") == "field"


def test_find_status_column_by_name_then_enum():
    cols = [_col("id", "int"), _col("state", "varchar"), _col("kind", category="enum")]
    assert casegen._find_status_column(cols) == "state"          # name wins
    cols2 = [_col("id", "int"), _col("kind", category="enum")]
    assert casegen._find_status_column(cols2) == "kind"          # falls back to enum
    assert casegen._find_status_column([_col("id", "int")]) is None


def test_detect_workflow_tables():
    schema = [
        {"table": "orders", "columns": [_col("id", "int"), _col("status", "varchar")]},
        {"table": "logs", "columns": [_col("msg", "text")]},
    ]
    cands = casegen.detect_workflow_tables(schema)
    assert [c["table"] for c in cands] == ["orders"]
    assert cands[0]["status_column"] == "status"


def test_enum_fallback_skips_compliance_flagged_columns():
    # A low-cardinality "zip" column classifies enum but is PII by name hint —
    # it must never be auto-picked as the status column.
    cols = [_col("id", "int"), _col("zip", category="enum"), _col("kind", category="enum")]
    assert casegen._find_status_column(cols) == "kind"
    assert casegen._find_status_column([_col("id", "int"), _col("zip", category="enum")]) is None


def test_status_column_problem_guards():
    cols = {
        "status": _col("status", category="enum"),
        "ssn": _col("ssn", category="ssn"),
        "full_name": _col("full_name", category="text"),
        "notes": _col("notes", category="free_text"),
    }
    distinct = {"status": ["NEW", "DONE"], "ssn": ["123-45-6789"],
                "full_name": ["Alice A"], "notes": [str(i) for i in range(30)]}
    assert casegen._status_column_problem("status", cols, distinct) is None
    # PII by value classification, PII by name hint, high cardinality, missing column:
    assert "compliance-flagged" in casegen._status_column_problem("ssn", cols, distinct)
    assert "compliance-flagged" in casegen._status_column_problem("full_name", cols, distinct)
    assert "too many distinct" in casegen._status_column_problem("notes", cols, distinct)
    assert "not found" in casegen._status_column_problem("ghost", cols, distinct)


def test_field_type_mapping():
    assert casegen._field_type(_col("email", category="email"), None) == "email"
    assert casegen._field_type(_col("dob", "date"), None) == "date"
    assert casegen._field_type(_col("flag", "tinyint"), None) == "boolean"
    assert casegen._field_type(_col("amount", "decimal"), None) == "number"
    assert casegen._field_type(_col("kind", category="enum"), ["A", "B"]) == "select"
    assert casegen._field_type(_col("notes", category="free_text"), None) == "textarea"
    assert casegen._field_type(_col("misc", "varchar"), None) == "text"
