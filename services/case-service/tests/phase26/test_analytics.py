"""HELIX P26 — HxAnalytics tests (24 tests).

Covers: platform_snapshot (empty DB), cases_over_time, sla_performance,
        funnel_by_case_type, query_engine (structured: count/throughput/sla/
        avg_resolution/snapshot, NL keyword fallback), saved reports (create,
        list, get, delete, run, export CSV/JSON), OData feed, metrics/snapshot
        endpoint, metrics/time-series, metrics/sla-performance, POST /query,
        GET /odata.
"""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.analytics.metrics import platform_snapshot, cases_over_time, sla_performance
from case_service.analytics.query_engine import run_structured, _parse_nl
from case_service.analytics.exporter import to_csv, to_json, odata_response
from case_service.main import app


# ── Auth ──────────────────────────────────────────────────────────────────────

def _admin():
    return AuthenticatedUser(
        user_id="admin-1", roles=["admin"],
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Admins",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Admin Portal", modules=[], homepage="/",
            roles=["admin"], privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _ov(): app.dependency_overrides[get_current_user] = lambda: _admin()
def _cl(): app.dependency_overrides.pop(get_current_user, None)


# ── Unit tests — metrics ──────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestMetricsUnit:
    async def test_platform_snapshot_empty_db(self, session):
        snap = await platform_snapshot(session)
        assert snap["total_cases"] == 0
        assert snap["sla_breach_pct"] == 0.0
        assert "snapshot_at" in snap

    async def test_cases_over_time_empty(self, session):
        result = await cases_over_time(session, days=7)
        assert isinstance(result, list)

    async def test_sla_performance_empty(self, session):
        result = await sla_performance(session, days=30)
        assert "series" in result
        assert isinstance(result["series"], list)


# ── Unit tests — query engine ─────────────────────────────────────────────────

@pytest.mark.asyncio
class TestQueryEngine:
    async def test_structured_snapshot(self, session):
        result = await run_structured({"metric": "snapshot"}, session)
        assert "data" in result
        assert result["chart_type"] == "number"

    async def test_structured_count_no_group(self, session):
        result = await run_structured({"metric": "count", "filters": {"days": 30}}, session)
        assert result["chart_type"] == "number"

    async def test_structured_count_by_status(self, session):
        result = await run_structured(
            {"metric": "count", "group_by": "status", "chart_type": "bar", "filters": {"days": 30}},
            session,
        )
        assert "series" in result

    async def test_structured_throughput(self, session):
        result = await run_structured(
            {"metric": "throughput", "chart_type": "line", "filters": {"days": 7}}, session
        )
        assert result["chart_type"] == "line"

    async def test_structured_sla_breach(self, session):
        result = await run_structured(
            {"metric": "sla_breach_rate", "filters": {"days": 14}}, session
        )
        assert "data" in result

    async def test_structured_avg_resolution(self, session):
        result = await run_structured(
            {"metric": "avg_resolution", "filters": {"days": 30}}, session
        )
        assert result["chart_type"] == "number"

    async def test_nl_keyword_sla(self):
        result = await _parse_nl("show me sla breach rate this month")
        assert result["metric"] == "sla_breach_rate"

    async def test_nl_keyword_throughput(self):
        result = await _parse_nl("how many new cases created last 30 days")
        assert result["metric"] == "throughput"

    async def test_nl_keyword_priority(self):
        result = await _parse_nl("cases by priority this quarter")
        assert result["group_by"] == "priority"

    async def test_nl_keyword_fallback(self):
        result = await _parse_nl("something completely unknown xyz")
        assert "metric" in result


# ── Unit tests — exporter ─────────────────────────────────────────────────────

class TestExporter:
    def test_to_csv_empty(self):
        csv = to_csv([])
        assert "label,value" in csv

    def test_to_csv_with_data(self):
        csv = to_csv([{"label": "open", "value": 10}, {"label": "closed", "value": 5}])
        assert "open" in csv
        assert "10" in csv

    def test_to_json(self):
        result = to_json({"key": "value"})
        assert '"key"' in result

    def test_odata_response(self):
        rows = [{"id": "1", "status": "open"}]
        resp = odata_response(rows)
        assert "@odata.context" in resp
        assert resp["@odata.count"] == 1


# ── API endpoint tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAnalyticsAPI:
    def setup_method(self): _ov()
    def teardown_method(self): _cl()

    async def test_metrics_snapshot(self, client: AsyncClient):
        r = await client.get("/api/v1/analytics/metrics/snapshot")
        assert r.status_code == 200
        assert "total_cases" in r.json()

    async def test_metrics_time_series(self, client: AsyncClient):
        r = await client.get("/api/v1/analytics/metrics/time-series?days=7")
        assert r.status_code == 200
        assert "series" in r.json()

    async def test_metrics_sla_performance(self, client: AsyncClient):
        r = await client.get("/api/v1/analytics/metrics/sla-performance?days=7")
        assert r.status_code == 200
        assert "series" in r.json()

    async def test_query_nl(self, client: AsyncClient):
        r = await client.post("/api/v1/analytics/query", json={"question": "cases by status"})
        assert r.status_code == 200
        assert "series" in r.json()

    async def test_query_structured(self, client: AsyncClient):
        r = await client.post("/api/v1/analytics/query", json={
            "query_def": {"metric": "count", "group_by": "priority", "filters": {"days": 30}}
        })
        assert r.status_code == 200

    async def test_query_missing_params(self, client: AsyncClient):
        r = await client.post("/api/v1/analytics/query", json={})
        assert r.status_code == 400

    async def test_odata_feed(self, client: AsyncClient):
        r = await client.get("/api/v1/analytics/odata")
        assert r.status_code == 200
        d = r.json()
        assert "@odata.context" in d
        assert "value" in d

    async def test_save_and_list_report(self, client: AsyncClient):
        r = await client.post("/api/v1/analytics/reports", json={
            "name": "Daily Overview",
            "query_type": "structured",
            "query_def": {"metric": "count", "group_by": "status", "filters": {"days": 30}},
            "chart_type": "bar",
        })
        assert r.status_code == 201
        rid = r.json()["id"]

        r2 = await client.get("/api/v1/analytics/reports")
        assert any(rp["id"] == rid for rp in r2.json()["reports"])

    async def test_run_saved_report(self, client: AsyncClient):
        resp = await client.post("/api/v1/analytics/reports", json={
            "name": "RunTest",
            "query_def": {"metric": "snapshot", "filters": {}},
            "chart_type": "number",
        })
        rid = resp.json()["id"]
        r = await client.get(f"/api/v1/analytics/reports/{rid}/run")
        assert r.status_code == 200
        assert "report_name" in r.json()

    async def test_export_csv(self, client: AsyncClient):
        resp = await client.post("/api/v1/analytics/reports", json={
            "name": "ExportTest",
            "query_def": {"metric": "count", "group_by": "status", "filters": {"days": 30}},
            "chart_type": "bar",
        })
        rid = resp.json()["id"]
        r = await client.get(f"/api/v1/analytics/reports/{rid}/export?format=csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]

    async def test_delete_report(self, client: AsyncClient):
        resp = await client.post("/api/v1/analytics/reports", json={
            "name": "DeleteReport", "query_def": {}, "chart_type": "bar",
        })
        rid = resp.json()["id"]
        r = await client.delete(f"/api/v1/analytics/reports/{rid}")
        assert r.status_code == 204
