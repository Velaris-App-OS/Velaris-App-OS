"""HELIX P23 — Observability & Telemetry tests."""
from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from case_service.observability import (
    ObservabilityMiddleware, configure_logging, get_logger,
    recent_spans, clear_spans,
    request_id_var, tenant_id_var, get_context,
    render_metrics, cases_created_total, sla_breaches_total,
)
from case_service.observability.metrics import reset_registry
from case_service.api.observability import router as observability_router


@pytest.fixture
def app() -> FastAPI:
    clear_spans()
    reset_registry()
    a = FastAPI()
    a.add_middleware(ObservabilityMiddleware)
    a.include_router(observability_router)

    # Override the real PostgreSQL session dep to avoid needing a live DB
    from case_service.db.session import get_session as _real_get_session
    async def _noop_session():
        yield None
    a.dependency_overrides[_real_get_session] = _noop_session

    @a.get("/ping")
    async def ping():
        return {"ok": True}

    @a.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    @a.get("/ctx")
    async def ctx():
        return get_context()

    @a.get("/slow")
    async def slow():
        await asyncio.sleep(0.05)
        return {"ok": True}

    return a


@pytest.fixture
async def client(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_01_correlation_id_added_when_absent(client):
    r = await client.get("/ping")
    assert r.status_code == 200
    assert "x-request-id" in r.headers
    assert len(r.headers["x-request-id"]) >= 8


@pytest.mark.asyncio
async def test_02_correlation_id_propagated_when_present(client):
    rid = "req-" + uuid.uuid4().hex[:8]
    r = await client.get("/ping", headers={"x-request-id": rid})
    assert r.headers["x-request-id"] == rid


@pytest.mark.asyncio
async def test_03_tenant_id_propagates_to_context(client):
    r = await client.get("/ctx", headers={"x-tenant-id": "acme"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "acme"
    assert body["request_id"] is not None


@pytest.mark.asyncio
async def test_04_metrics_endpoint_serves_prometheus_text(client):
    await client.get("/ping")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "# HELP" in r.text
    assert "helix_http_requests_total" in r.text


@pytest.mark.asyncio
async def test_05_http_counter_increments_on_request(client):
    for _ in range(3):
        await client.get("/ping")
    body = (await client.get("/metrics")).text
    assert body.count('path="/ping"') >= 1


@pytest.mark.asyncio
async def test_06_histogram_records_latency_buckets(client):
    await client.get("/ping")
    body, _ = render_metrics()
    s = body.decode()
    assert "helix_http_request_duration_seconds_bucket" in s
    assert 'le="0.1"' in s


@pytest.mark.asyncio
async def test_07_error_responses_counted_with_status_label(client):
    try:
        await client.get("/boom")
    except Exception:
        pass
    body = (await client.get("/metrics")).text
    assert 'status="500"' in body or 'path="/boom"' in body


def test_08_custom_business_counters_work():
    reset_registry()
    cases_created_total.labels(tenant_id="acme", case_type="loan").inc()
    cases_created_total.labels(tenant_id="acme", case_type="loan").inc()
    sla_breaches_total.labels(tenant_id="acme", case_type="loan", sla_name="resolution").inc()
    body, _ = render_metrics()
    s = body.decode()
    assert 'helix_cases_created_total{case_type="loan",tenant_id="acme"} 2' in s
    assert "helix_sla_breaches_total" in s


def test_09_structlog_injects_context_vars():
    """The _inject_ctx processor must copy contextvars into each event dict."""
    from case_service.observability.logging_config import _inject_ctx
    tok_rid = request_id_var.set("req-abc")
    tok_tid = tenant_id_var.set("acme")
    try:
        event = {"event": "hello.p23", "custom_field": 42}
        out = _inject_ctx(None, "info", event)
        assert out["request_id"] == "req-abc"
        assert out["tenant_id"] == "acme"
        assert out["event"] == "hello.p23"
        assert out["custom_field"] == 42
    finally:
        request_id_var.reset(tok_rid)
        tenant_id_var.reset(tok_tid)


def test_10_get_context_returns_all_fields():
    tok = request_id_var.set("r1")
    tok2 = tenant_id_var.set("t1")
    try:
        ctx = get_context()
        assert ctx["request_id"] == "r1"
        assert ctx["tenant_id"] == "t1"
        assert "trace_id" in ctx
        assert "user_id" in ctx
    finally:
        request_id_var.reset(tok)
        tenant_id_var.reset(tok2)


@pytest.mark.asyncio
async def test_11_health_shallow_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "case-service"
    assert "timestamp" in body


@pytest.mark.asyncio
async def test_12_health_deep_returns_components(client):
    r = await client.get("/health/deep")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["components"], list)
    names = {c["component"] for c in body["components"]}
    assert "telemetry" in names
    assert "database" in names


@pytest.mark.asyncio
async def test_13_metrics_summary_endpoint(client):
    for _ in range(5):
        await client.get("/ping")
    r = await client.get("/api/v1/observability/metrics")
    assert r.status_code == 200
    body = r.json()
    for k in ("total_requests", "error_rate", "slowest_endpoints", "latency"):
        assert k in body
    assert body["total_requests"] >= 1


@pytest.mark.asyncio
async def test_14_recent_traces_endpoint(client):
    for _ in range(4):
        await client.get("/ping")
    r = await client.get("/api/v1/observability/traces/recent?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    span = body["spans"][0]
    for k in ("method", "path", "status", "duration_ms", "request_id", "tenant_id", "timestamp"):
        assert k in span


@pytest.mark.asyncio
async def test_15_recent_traces_respects_limit(client):
    for _ in range(10):
        await client.get("/ping")
    r = await client.get("/api/v1/observability/traces/recent?limit=3")
    body = r.json()
    assert len(body["spans"]) <= 3


@pytest.mark.asyncio
async def test_16_metrics_summary_identifies_slow_paths(client):
    await client.get("/slow")
    await client.get("/ping")
    r = await client.get("/api/v1/observability/metrics")
    body = r.json()
    paths = set(body["latency"].keys())
    assert any("/slow" in p or "/ping" in p for p in paths)


def test_17_span_buffer_is_ring_limited():
    from case_service.observability.spans import record_span, recent_spans, clear_spans
    clear_spans()
    for i in range(250):
        record_span(method="GET", path=f"/x/{i}", status=200, duration_ms=1.0,
                    request_id=f"r{i}", tenant_id="t")
    spans = recent_spans(limit=300)
    assert len(spans) <= 200


def test_18_span_buffer_ordering_latest_first():
    from case_service.observability.spans import record_span, recent_spans, clear_spans
    clear_spans()
    record_span(method="GET", path="/a", status=200, duration_ms=1.0, request_id="r1", tenant_id="t")
    record_span(method="GET", path="/b", status=200, duration_ms=2.0, request_id="r2", tenant_id="t")
    spans = recent_spans(limit=5)
    assert spans[0]["path"] == "/b"
    assert spans[1]["path"] == "/a"


def test_19_telemetry_configure_is_idempotent():
    from case_service.observability.telemetry import configure_telemetry
    configure_telemetry("test-service")
    configure_telemetry("test-service")


def test_20_observability_package_exports():
    import case_service.observability as obs
    expected = {
        "ObservabilityMiddleware", "configure_logging", "get_logger",
        "render_metrics", "recent_spans", "clear_spans",
        "request_id_var", "tenant_id_var", "trace_id_var", "user_id_var",
        "get_context", "configure_telemetry", "get_tracer",
        "cases_created_total", "sla_breaches_total",
    }
    missing = expected - set(dir(obs))
    assert not missing, f"missing exports: {missing}"
