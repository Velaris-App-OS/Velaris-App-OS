"""
helix_engine.temporal — Temporal.io Integration
================================================

Bridges the BPMN engine with Temporal's durable execution.

Modules::

    client.py      → Temporal client connection (shared singleton)
    worker.py      → Temporal worker (listens on "helix-engine" task queue)
    workflows/     → @workflow.defn  — the BPMN process executor
    activities/    → @activity.defn  — task execution (I/O happens here)

Connection: localhost:7233, namespace "default" (configurable via env vars).
"""
