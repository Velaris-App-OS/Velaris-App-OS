"""MariaDB introspection — dispatch + statement-timeout SQL (DB-free unit tests).

The live behaviour (schema build, timeout applied, HxDBManager endpoints) is covered by
running phase_dbsdk + phase67 against a real MariaDB container in CI. These unit tests pin
the two things that make MariaDB distinct from MySQL:
  * dispatch routes a MariaDB bind (dialect.name == "mysql", _is_mariadb True) to the
    MariaDB introspector — even when config says mysql;
  * set/reset_statement_timeout emits MariaDB's max_statement_time (seconds), not MySQL's
    max_execution_time (ms).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from case_service.db.introspection import get_introspector
from case_service.db.introspection.mariadb import MariadbIntrospector
from case_service.db.introspection.mysql import MysqlIntrospector


class _FakeBind:
    def __init__(self, name, is_mariadb):
        self.dialect = SimpleNamespace(name=name, _is_mariadb=is_mariadb)


class _FakeSession:
    def __init__(self, name, is_mariadb):
        self._bind = _FakeBind(name, is_mariadb)

    def get_bind(self):
        return self._bind


class _RecordingSession:
    def __init__(self):
        self.sql = []

    async def execute(self, statement, *a, **k):
        self.sql.append(str(statement))
        return None


# ── dispatch ──────────────────────────────────────────────────────────────────

def test_mariadb_bind_routes_to_mariadb_introspector():
    insp = get_introspector(_FakeSession("mysql", is_mariadb=True))
    assert isinstance(insp, MariadbIntrospector)


def test_mysql_bind_routes_to_mysql_introspector():
    insp = get_introspector(_FakeSession("mysql", is_mariadb=False))
    assert isinstance(insp, MysqlIntrospector)
    assert not isinstance(insp, MariadbIntrospector)


# ── statement timeout SQL (the one real divergence) ───────────────────────────

def test_mariadb_set_timeout_uses_max_statement_time_seconds():
    sess = _RecordingSession()
    asyncio.run(MariadbIntrospector().set_statement_timeout(sess, 30000))  # 30000 ms
    assert sess.sql == ["SET SESSION max_statement_time = 30.0"]  # seconds, not ms


def test_mariadb_reset_timeout():
    sess = _RecordingSession()
    asyncio.run(MariadbIntrospector().reset_statement_timeout(sess))
    assert sess.sql == ["SET SESSION max_statement_time = 0"]


def test_mysql_still_uses_max_execution_time_ms():
    # Guard against accidentally changing the MySQL path while touching MariaDB.
    sess = _RecordingSession()
    asyncio.run(MysqlIntrospector().set_statement_timeout(sess, 30000))
    assert sess.sql == ["SET SESSION max_execution_time = 30000"]  # ms, unchanged
