from __future__ import annotations
from prometheus_client import (
    Counter, Histogram, Gauge, CollectorRegistry,
    generate_latest, CONTENT_TYPE_LATEST,
)

registry = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "helix_http_requests_total", "Total HTTP requests",
    ["method", "path", "status", "tenant_id"], registry=registry,
)
http_request_duration_seconds = Histogram(
    "helix_http_request_duration_seconds", "HTTP request latency (seconds)",
    ["method", "path"], registry=registry,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
cases_created_total = Counter(
    "helix_cases_created_total", "Cases created",
    ["tenant_id", "case_type"], registry=registry,
)
sla_breaches_total = Counter(
    "helix_sla_breaches_total", "SLA breaches",
    ["tenant_id", "case_type", "sla_name"], registry=registry,
)
webhooks_delivered_total = Counter(
    "helix_webhooks_delivered_total", "Webhooks delivered",
    ["tenant_id", "status"], registry=registry,
)
active_cases_gauge = Gauge(
    "helix_active_cases", "Active cases",
    ["tenant_id"], registry=registry,
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(registry), CONTENT_TYPE_LATEST


def reset_registry() -> None:
    for m in (
        http_requests_total, http_request_duration_seconds,
        cases_created_total, sla_breaches_total, webhooks_delivered_total,
        active_cases_gauge,
    ):
        try:
            m._metrics.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
