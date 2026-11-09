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
    # tenant_id (2026-11-04): the signed-in caller's, so a tenant's admin can be
    # shown its own latencies. A new label; summing over it gives the old series.
    ["method", "path", "tenant_id"], registry=registry,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
cases_created_total = Counter(
    "helix_cases_created_total", "Cases created",
    ["tenant_id", "case_type"], registry=registry,
)
# R4/R7: 1 while an optional dependency is meant to be on and is not working
# (case_service/readiness.py), 0 otherwise — off by choice is 0, never an alert.
# Refreshed by /metrics itself, so every process answers it.
dependency_degraded = Gauge(
    "velaris_dependency_degraded", "1 = this dependency is degraded (see Studio > Status)",
    ["dependency"], registry=registry,
)
sla_breaches_total = Counter(
    "helix_sla_breaches_total", "SLA breaches",
    ["tenant_id", "case_type", "sla_name"], registry=registry,
)
webhooks_delivered_total = Counter(
    "helix_webhooks_delivered_total", "Webhooks delivered",
    ["tenant_id", "status"], registry=registry,
)
# ── HxArbiter ─────────────────────────────────────────────────────────────
#
# §13 sets latency targets for the engine and nothing emitted a metric, so an
# operator could not alert on any of the three things that actually go wrong:
# a namespace suddenly denying, PIPs timing out, or the log not being sealed.
#
# `namespace` and not `action` as a label: actions are unbounded (a PEP names
# its own), and an unbounded label is how a metrics endpoint becomes the
# outage. Namespaces are a registry.
arbiter_decisions_total = Counter(
    "helix_arbiter_decisions_total", "HxArbiter policy decisions",
    # `enforced` distinguishes a decision that changed an answer from one that
    # only observed — without it a shadow rollout looks like an enforcing one.
    ["tenant_id", "namespace", "effect", "enforced"], registry=registry,
)
arbiter_decision_seconds = Histogram(
    "helix_arbiter_decision_seconds", "HxArbiter decision latency (seconds)",
    ["namespace"], registry=registry,
    # Tighter than the HTTP buckets: §13's target is p99 < 2ms, and the default
    # buckets start at 5ms — every decision would land in the first one and the
    # histogram would say nothing.
    buckets=(0.0001, 0.00025, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.05, 0.1),
)
arbiter_decision_errors_total = Counter(
    "helix_arbiter_decision_errors_total",
    "Decisions the engine could not evaluate (pip_timeout, policy_load_failed…)",
    ["namespace", "error"], registry=registry,
)
arbiter_legacy_disagreements_total = Counter(
    "helix_arbiter_legacy_disagreements_total",
    "Decisions where the engine and the control it shadows disagreed",
    # Direction matters more than the count: `engine_weaker` is a hole and must
    # be zero before a promotion; `engine_stricter` is an outage if promoted.
    ["namespace", "direction"], registry=registry,
)
arbiter_unsealed_decisions = Gauge(
    "helix_arbiter_unsealed_decisions", "Decisions not yet sealed into the chain",
    registry=registry,
)

# R5d 11b — where opted-in reads ran: replica | main_no_replica | main_own_writes
# | main_replica_down | main_barrier. A replica that keeps being skipped shows here.
db_reads_total = Counter(
    "velaris_db_reads_total",
    "Opted-in reads by where they ran (R5d read/write split)",
    ["route"],
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
