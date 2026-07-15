"""HxDBMigrate P4 — data-migration PII exclusion logic (DB-free units).

The live migrate/dry-run path is exercised in the P4 live test; these pin the security-
critical part: which columns are dropped for each pii_mode.
"""
from __future__ import annotations

from case_service.hxdbmigrate import migrate as M


# rows with ordinary PII (email) and high-sensitivity PII (ssn, card_number)
_COLUMNS = ["id", "email", "ssn", "card_number", "notes"]
_ROWS = [
    {"id": 1, "email": "a@x.com", "ssn": "123-45-6789", "card_number": "4111 1111 1111 1111", "notes": "hi"},
    {"id": 2, "email": "b@y.org", "ssn": "987-65-4321", "card_number": "5500 0000 0000 0004", "notes": "yo"},
    {"id": 3, "email": "c@z.net", "ssn": "111-22-3333", "card_number": "4012 8888 8888 1881", "notes": "ok"},
]


def test_pii_modes_list():
    assert M.pii_modes() == ["safe", "exclude_all", "as_is"]


def test_safe_excludes_only_tokenize_columns():
    # safe drops cards/SSNs (tokenize) but keeps ordinary PII like email.
    ex = M._exclusions(_COLUMNS, _ROWS, "safe")
    assert "ssn" in ex and "card_number" in ex
    assert "email" not in ex and "id" not in ex


def test_exclude_all_drops_every_flagged_column():
    ex = M._exclusions(_COLUMNS, _ROWS, "exclude_all")
    assert {"ssn", "card_number", "email"} <= ex


def test_as_is_excludes_nothing():
    assert M._exclusions(_COLUMNS, _ROWS, "as_is") == set()


def test_migrate_target_case_type_tenant_scoped():
    # Migration may target a global case-type or the caller's own — never another tenant's.
    from case_service.api.routers.hxdbmigrate import _case_type_visible
    assert _case_type_visible(None, "default") is True
    assert _case_type_visible("acme", "acme") is True
    assert _case_type_visible("acme", "default") is False
    assert _case_type_visible("other-tenant", "acme") is False
