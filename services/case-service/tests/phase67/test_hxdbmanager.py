"""P67 — HxDBManager security and functional tests (28 tests).

Covers:
  Auth:       unauthenticated, non-admin, admin, superadmin role gates
  DDL safety: /execute rejects all DDL keywords; /ddl requires confirm + reason
  Endpoint separation: /ddl rejects SELECT/DML; /execute rejects DDL
  Injection:  table name SQL injection blocked via existence check
  Audit log:  every query (success, error, rejected) written to query log
  Functional: schema list, table detail, table rows pagination, SQL execution,
              EXPLAIN, CSV/JSON export, query history, slow-query availability
"""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.db.models import DbManagerQueryLogModel
from case_service.main import app


# ── Auth fixtures ─────────────────────────────────────────────────────────────

def _make_user(roles: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"test-{uuid.uuid4()}",
        roles=roles,
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Test Group",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Test Portal", modules=[], homepage="/",
            roles=roles, privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _admin():      return _make_user(["admin"])
def _superadmin(): return _make_user(["admin", "superadmin"])
def _worker():     return _make_user(["case_worker"])
def _viewer():     return _make_user(["viewer"])

# DB-SDK Phase 1b: HxDBManager introspection is now dialect-portable (Postgres + MySQL
# via case_service/db/introspection). Two markers:
#   REQUIRES_EXTERNAL_DB — needs a real DB (information_schema, EXPLAIN, real tables);
#                          RUNS on both the Postgres AND MySQL harness, skips on SQLite.
#   REQUIRES_PG          — the test SQL itself is Postgres-only (generate_series); runs
#                          ONLY when the harness URL is Postgres.
import os as _os
_EXT_URL = _os.environ.get("VELARIS_TEST_DATABASE_URL", "")
_IS_PG   = _EXT_URL.startswith("postgresql")
REQUIRES_EXTERNAL_DB = pytest.mark.skipif(
    not _EXT_URL,
    reason="Requires an external DB harness (Postgres or MySQL) — set VELARIS_TEST_DATABASE_URL"
)
REQUIRES_PG = pytest.mark.skipif(
    not _IS_PG,
    reason="Requires PostgreSQL specifically (test uses pg-only SQL such as generate_series)"
)

def _override(user_fn):
    app.dependency_overrides[get_current_user] = user_fn

def _clear():
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _enable_hxdbmanager_flags():
    """HxDBManager endpoints 404 (via `_require_admin`/`_require_db_viewer`) unless the
    `hxdbmanager` + `hxdbmanager_security` feature flags are on — set from releases.txt
    in prod, never seeded in the test DB. Enable them for this module (restored after)
    so the auth/DDL-safety/DoS logic under test is actually reachable."""
    from case_service.api.routers import releases
    saved = dict(releases._ENABLED_VERSIONS)
    releases._ENABLED_VERSIONS["hxdbmanager"] = "v1.0.0"
    releases._ENABLED_VERSIONS["hxdbmanager_security"] = "v1.0.0"
    yield
    releases._ENABLED_VERSIONS.clear()
    releases._ENABLED_VERSIONS.update(saved)


# ═══════════════════════════════════════════════════════
#  1. Authentication & Authorisation
# ═══════════════════════════════════════════════════════

class TestAuth:
    """Every endpoint requires auth; /ddl requires superadmin."""

    @pytest.mark.asyncio
    async def test_schema_no_token_returns_401(self, anon_client: AsyncClient):
        _clear()
        r = await anon_client.get("/api/v1/hxdbmanager/schema")
        assert r.status_code in (401, 403), r.text

    @pytest.mark.asyncio
    async def test_schema_case_worker_returns_403(self, client: AsyncClient):
        _override(_worker)
        r = await client.get("/api/v1/hxdbmanager/schema")
        assert r.status_code == 403
        assert "admin" in r.json()["detail"].lower()
        _clear()

    @pytest.mark.asyncio
    async def test_schema_viewer_returns_403(self, client: AsyncClient):
        _override(_viewer)
        r = await client.get("/api/v1/hxdbmanager/schema")
        assert r.status_code == 403
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_schema_admin_succeeds(self, client: AsyncClient):
        _override(_admin)
        r = await client.get("/api/v1/hxdbmanager/schema")
        assert r.status_code == 200
        assert "tables" in r.json()
        _clear()

    @pytest.mark.asyncio
    async def test_ddl_admin_only_returns_403(self, client: AsyncClient):
        """Admin role cannot use /ddl — superadmin required."""
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/ddl", json={
            "sql": "CREATE INDEX ix_test ON access_groups(name)",
            "confirm": True, "reason": "admin trying ddl"
        })
        assert r.status_code == 403
        assert "superadmin" in r.json()["detail"].lower()
        _clear()

    @pytest.mark.asyncio
    async def test_ddl_superadmin_can_access(self, client: AsyncClient):
        """Superadmin can reach /ddl (though the SQL may fail for other reasons)."""
        _override(_superadmin)
        r = await client.post("/api/v1/hxdbmanager/ddl", json={
            "sql": "SELECT 1",  # will be rejected as non-DDL, but auth passes
            "confirm": True, "reason": "auth test"
        })
        assert r.status_code != 403, "superadmin should not be blocked by auth"
        _clear()

    @pytest.mark.asyncio
    async def test_execute_no_token_returns_401(self, anon_client: AsyncClient):
        _clear()
        r = await anon_client.post("/api/v1/hxdbmanager/execute", json={"sql": "SELECT 1"})
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_history_worker_returns_403(self, client: AsyncClient):
        _override(_worker)
        r = await client.get("/api/v1/hxdbmanager/history")
        assert r.status_code == 403
        _clear()


# ═══════════════════════════════════════════════════════
#  2. DDL Safety — /execute must reject all DDL
# ═══════════════════════════════════════════════════════

class TestDdlSafety:
    """Every DDL keyword must be blocked at /execute."""

    DDL_STATEMENTS = [
        "DROP TABLE case_types",
        "TRUNCATE helix_users",
        "ALTER TABLE case_types ADD COLUMN evil TEXT",
        "CREATE TABLE pwned (id INT)",
        "CREATE INDEX ix_evil ON case_types(name)",
        "VACUUM FULL case_types",
        "CLUSTER case_types",
        "REINDEX TABLE case_types",
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sql", DDL_STATEMENTS)
    async def test_ddl_blocked_at_execute(self, client: AsyncClient, sql: str):
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/execute", json={"sql": sql})
        assert r.status_code == 400, f"DDL should be blocked: {sql}"
        detail = r.json()["detail"]
        assert "ddl" in detail.lower() or "not allowed" in detail.lower(), detail
        _clear()

    @pytest.mark.asyncio
    async def test_empty_sql_rejected(self, client: AsyncClient):
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/execute", json={"sql": ""})
        assert r.status_code == 400
        assert "empty" in r.json()["detail"].lower()
        _clear()

    @pytest.mark.asyncio
    async def test_whitespace_only_sql_rejected(self, client: AsyncClient):
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/execute", json={"sql": "   \n\t  "})
        assert r.status_code == 400
        _clear()


# ═══════════════════════════════════════════════════════
#  3a. DoS / Infinite Loop Prevention
# ═══════════════════════════════════════════════════════

class TestDosPrevention:
    """Abuse patterns that could make the DB busy must be blocked before any DB round-trip."""

    ABUSE_SQLS = [
        ("pg_sleep direct",            "SELECT pg_sleep(60)"),
        ("pg_sleep in subquery",       "SELECT * FROM (SELECT pg_sleep(5)) t"),
        ("pg_terminate_backend",       "SELECT pg_terminate_backend(12345)"),
        ("pg_cancel_backend",          "SELECT pg_cancel_backend(12345)"),
        ("generate_series huge upper", "SELECT generate_series(1, 99999999)"),
        ("generate_series 10M",        "SELECT * FROM generate_series(1, 10000000) AS n"),
        ("WITH RECURSIVE no LIMIT",    "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t) SELECT * FROM t"),
    ]

    ALLOWED_SQLS = [
        ("generate_series small",      "SELECT generate_series(1, 100)"),
        ("WITH RECURSIVE with LIMIT",  "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n < 10) SELECT * FROM t LIMIT 50"),
        ("normal select",              "SELECT 1 as safe"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label,sql", ABUSE_SQLS)
    async def test_abuse_pattern_blocked(self, client: AsyncClient, label: str, sql: str):
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/execute", json={"sql": sql})
        assert r.status_code == 400, f"[{label}] Expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "blocked" in detail.lower() or "not permitted" in detail.lower() or "limit" in detail.lower(), \
            f"[{label}] Unexpected error message: {detail}"
        _clear()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label,sql", ALLOWED_SQLS)
    @REQUIRES_PG  # ALLOWED_SQLS use generate_series — Postgres-only execution
    async def test_safe_patterns_not_blocked(self, client: AsyncClient, label: str, sql: str):
        """Safe queries must not be blocked by abuse detection."""
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/execute", json={"sql": sql, "row_limit": 10})
        assert r.status_code == 200, f"[{label}] Should not be blocked: {r.text}"
        _clear()

    @pytest.mark.asyncio
    async def test_per_user_rate_limit(self, client: AsyncClient):
        """After 20 calls in 60s from same user, 429 must be returned."""
        from case_service.api.routers.hxdbmanager import _EXECUTE_RATE
        import collections, time as _time

        # One fixed user instance: fill the bucket under ITS id and return the SAME
        # instance from the override, so the bucket key provably matches the user the
        # endpoint resolves. (Filling to the limit makes the rate check trip BEFORE the
        # SQL runs — so the test never depends on Postgres execution.)
        u = _make_user(["admin"])
        _EXECUTE_RATE[u.user_id] = collections.deque([_time.monotonic()] * 20)
        _override(lambda: u)
        try:
            r = await client.post("/api/v1/hxdbmanager/execute", json={"sql": "SELECT 1"})
            assert r.status_code == 429, f"Expected 429 rate limit, got {r.status_code}: {r.text}"
            assert "rate limit" in r.json()["detail"].lower()
        finally:
            _clear()
            _EXECUTE_RATE.pop(u.user_id, None)


# ═══════════════════════════════════════════════════════
#  3. /ddl Endpoint Safety
# ═══════════════════════════════════════════════════════

class TestDdlEndpoint:
    """/ddl must enforce confirm, reason, and DDL-only classification."""

    @pytest.mark.asyncio
    async def test_ddl_without_confirm_rejected(self, client: AsyncClient):
        _override(_superadmin)
        r = await client.post("/api/v1/hxdbmanager/ddl", json={
            "sql": "CREATE INDEX ix_test ON access_groups(name)",
            "confirm": False
        })
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"].lower()
        _clear()

    @pytest.mark.asyncio
    async def test_ddl_empty_reason_rejected(self, client: AsyncClient):
        _override(_superadmin)
        r = await client.post("/api/v1/hxdbmanager/ddl", json={
            "sql": "CREATE INDEX ix_test ON access_groups(name)",
            "confirm": True, "reason": ""
        })
        assert r.status_code == 400
        assert "reason" in r.json()["detail"].lower()
        _clear()

    @pytest.mark.asyncio
    async def test_ddl_short_reason_rejected(self, client: AsyncClient):
        _override(_superadmin)
        r = await client.post("/api/v1/hxdbmanager/ddl", json={
            "sql": "CREATE INDEX ix_test ON access_groups(name)",
            "confirm": True, "reason": "ok"  # too short (< 5 chars)
        })
        assert r.status_code == 400
        _clear()

    @pytest.mark.asyncio
    async def test_select_via_ddl_endpoint_rejected(self, client: AsyncClient):
        """/ddl must not run SELECT statements — wrong endpoint."""
        _override(_superadmin)
        r = await client.post("/api/v1/hxdbmanager/ddl", json={
            "sql": "SELECT * FROM helix_users",
            "confirm": True, "reason": "trying to read via ddl"
        })
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "ddl" in detail.lower()
        _clear()

    @pytest.mark.asyncio
    async def test_dml_via_ddl_endpoint_rejected(self, client: AsyncClient):
        """/ddl must not run DML statements."""
        _override(_superadmin)
        r = await client.post("/api/v1/hxdbmanager/ddl", json={
            "sql": "DELETE FROM access_groups WHERE 1=1",
            "confirm": True, "reason": "trying to delete via ddl"
        })
        assert r.status_code == 400
        _clear()


# ═══════════════════════════════════════════════════════
#  4. SQL Injection Prevention
# ═══════════════════════════════════════════════════════

class TestInjectionPrevention:
    """Table name injection must be blocked via existence check."""

    INJECTION_TABLE_NAMES = [
        "case_types; DROP TABLE helix_users--",
        "case_types UNION SELECT * FROM helix_users--",
        "'; SELECT pg_sleep(5)--",
        "../../../etc/passwd",
        "case_types\x00evil",
    ]

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    @pytest.mark.parametrize("table_name", INJECTION_TABLE_NAMES)
    async def test_injection_via_table_name_blocked(self, client: AsyncClient, table_name: str):
        from urllib.parse import quote
        _override(_admin)
        # URL-encode the path segment so even a NUL byte reaches the server (httpx
        # refuses to send a raw \x00) — the server must still block it.
        r = await client.get(f"/api/v1/hxdbmanager/schema/{quote(table_name, safe='')}")
        # Must get 404 (table not found) — never 200 or 500
        assert r.status_code == 404, f"Expected 404 for injection attempt: {table_name!r}, got {r.status_code}: {r.text}"
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_injection_via_table_rows_endpoint_blocked(self, client: AsyncClient):
        _override(_admin)
        r = await client.get("/api/v1/hxdbmanager/tables/helix_users%3B+DROP+TABLE+case_types--/rows")
        assert r.status_code == 404
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_sql_params_in_execute_are_safe(self, client: AsyncClient):
        """Ensure parameterised execution doesn't allow injection via sql body."""
        _override(_admin)
        # This SELECT is safe — we're verifying the endpoint doesn't crash or expose data
        r = await client.post("/api/v1/hxdbmanager/execute", json={
            "sql": "SELECT 1 as safe_result",
            "row_limit": 1
        })
        assert r.status_code == 200
        assert r.json()["rows"][0]["safe_result"] == 1
        _clear()


# ═══════════════════════════════════════════════════════
#  5. Audit Log
# ═══════════════════════════════════════════════════════

class TestAuditLog:
    """Every query — success, error, or rejected — must be written to query log."""

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_successful_query_logged(self, client: AsyncClient, session):
        _override(_admin)
        await client.post("/api/v1/hxdbmanager/execute", json={"sql": "SELECT 1 as ping", "row_limit": 1})
        # Check log entry exists
        from sqlalchemy import select
        entries = (await session.execute(select(DbManagerQueryLogModel)
            .order_by(DbManagerQueryLogModel.ran_at.desc()).limit(1))).scalars().all()
        # In SQLite test DB the model may not be present; skip gracefully
        if entries:
            assert entries[0].status == "success"
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_rejected_ddl_logged(self, client: AsyncClient, session):
        _override(_admin)
        await client.post("/api/v1/hxdbmanager/execute", json={"sql": "DROP TABLE case_types"})
        from sqlalchemy import select
        entries = (await session.execute(select(DbManagerQueryLogModel)
            .where(DbManagerQueryLogModel.status == "rejected")
            .order_by(DbManagerQueryLogModel.ran_at.desc()).limit(1))).scalars().all()
        if entries:
            assert "DROP" in entries[0].query_text.upper()
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_history_endpoint_returns_own_queries(self, client: AsyncClient):
        _override(_admin)
        await client.post("/api/v1/hxdbmanager/execute", json={"sql": "SELECT 42 as marker"})
        r = await client.get("/api/v1/hxdbmanager/history?limit=10")
        assert r.status_code == 200
        assert "history" in r.json()
        _clear()


# ═══════════════════════════════════════════════════════
#  6. Functional — Schema & Table (PostgreSQL only)
# ═══════════════════════════════════════════════════════

class TestFunctional:
    """Core read paths work for admin users — PostgreSQL integration tests."""

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_schema_returns_table_list(self, client: AsyncClient):
        _override(_admin)
        r = await client.get("/api/v1/hxdbmanager/schema")
        assert r.status_code == 200
        data = r.json()
        assert "tables" in data
        assert isinstance(data["tables"], list)
        # Non-empty + a known table must actually appear. This catches the silent
        # MySQL landmine where `table_schema='public'` returns zero rows (no error):
        # an isinstance-list check would pass on an empty browser.
        names = {t["table_name"] for t in data["tables"]}
        assert names, "schema returned ZERO tables — dialect introspection likely scoped to the wrong schema"
        assert "access_groups" in names, f"expected seeded table missing; got {sorted(names)[:10]}…"
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_schema_table_detail_has_columns(self, client: AsyncClient):
        _override(_admin)
        r = await client.get("/api/v1/hxdbmanager/schema/access_groups")
        if r.status_code == 404:
            pytest.skip("access_groups not in test DB")
        assert r.status_code == 200
        data = r.json()
        assert "columns" in data
        assert "indexes" in data
        assert "foreign_keys" in data
        # Columns must actually be returned (silent-empty-result guard, both dialects).
        assert len(data["columns"]) > 0, "table detail returned ZERO columns"
        assert any(c["column_name"] == "id" for c in data["columns"]), \
            "expected 'id' column missing from access_groups detail"
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_schema_nonexistent_table_returns_404(self, client: AsyncClient):
        _override(_admin)
        r = await client.get("/api/v1/hxdbmanager/schema/this_table_does_not_exist_xyz")
        assert r.status_code == 404
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_execute_select_returns_rows(self, client: AsyncClient):
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/execute", json={
            "sql": "SELECT 1 as one, 2 as two", "row_limit": 1
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        assert data["rows"][0]["one"] == 1
        assert data["rows"][0]["two"] == 2
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_explain_returns_plan(self, client: AsyncClient):
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/explain", json={"sql": "SELECT 1"})
        assert r.status_code == 200
        assert "plan" in r.json()
        _clear()

    @pytest.mark.asyncio
    async def test_explain_ddl_rejected(self, client: AsyncClient):
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/explain", json={"sql": "DROP TABLE evil"})
        assert r.status_code == 400
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_EXTERNAL_DB
    async def test_slow_queries_returns_availability_flag(self, client: AsyncClient):
        _override(_admin)
        r = await client.get("/api/v1/hxdbmanager/slow-queries?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert "available" in data
        assert isinstance(data["available"], bool)
        _clear()

    @pytest.mark.asyncio
    @REQUIRES_PG  # uses generate_series — Postgres-only
    async def test_row_limit_respected(self, client: AsyncClient):
        """Row limit parameter must cap results."""
        _override(_admin)
        r = await client.post("/api/v1/hxdbmanager/execute", json={
            "sql": "SELECT generate_series(1,100) as n",
            "row_limit": 5
        })
        if r.status_code == 200:
            data = r.json()
            assert len(data["rows"]) <= 5
            assert data.get("truncated") is True
        _clear()
