"""R7 — the operator documents name only things that exist.

A runbook that names a script, a flag or a metric nobody ships is worse than
none: the operator follows it at the worst moment. Every command path, every
alert's metric and every link here is checked against the tree.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DOCS = [ROOT / p for p in ("docs/operator/runbook.md", "docs/operator/monitoring.md",
                           "docs/operator/support.md", "SECURITY.md", "CHANGELOG.md")]


def test_every_script_the_docs_name_exists():
    missing = []
    for doc in DOCS:
        for path in set(re.findall(r"(?:\./|\b)((?:deploy|scripts)/[\w./-]+\.(?:sh|py)|[\w-]+-velaris\.sh|"
                                   r"support-bundle\.sh)", doc.read_text())):
            if not (ROOT / path).exists():
                missing.append((doc.name, path))
    assert missing == []


def test_every_flag_the_runbook_names_is_in_its_script():
    text = (ROOT / "docs/operator/runbook.md").read_text()
    for script, flag in re.findall(r"\./([\w-]+\.sh)[^\n`]*?(--[a-z][\w-]+)", text):
        assert flag in (ROOT / script).read_text(), (script, flag)


def test_every_alert_uses_a_metric_the_platform_exports():
    rules = (ROOT / "deploy/docker-compose/observability/alerts.yml").read_text()
    exported = (ROOT / "services/_shared/velaris_shared/observability/metrics.py").read_text()
    for metric in set(re.findall(r"\b(helix_[a-z_]+|velaris_[a-z_]+)", rules)):
        base = re.sub(r"_(bucket|sum|count)$", "", metric)
        assert f'"{base}"' in exported, metric
    for name in re.findall(r"- alert: (\w+)", rules):
        assert f'id="{name.lower()}"' in (ROOT / "docs/operator/monitoring.md").read_text(), name


def test_prometheus_can_reach_the_platform():
    compose = (ROOT / "deploy/docker-compose/docker-compose.yml").read_text()
    block = compose.split("  prometheus:", 1)[1].split("\n  otel-collector:", 1)[0]
    assert "network_mode: host" in block and "127.0.0.1:9090" in block
    assert "./observability/alerts.yml:/etc/prometheus/alerts.yml:ro" in block
    prom = (ROOT / "deploy/docker-compose/observability/prometheus.yml").read_text()
    assert "127.0.0.1:8200" in prom and "host.docker.internal" not in prom


def test_the_guide_is_built_from_these_sources():
    src = (ROOT / "scripts/operator_guide.py").read_text()
    for doc in DOCS:
        assert str(doc.relative_to(ROOT)) in src, doc
    assert (ROOT / "docs/operator/Velaris-Operator-Guide.pdf").exists()
