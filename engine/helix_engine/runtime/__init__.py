"""
helix_engine.runtime — BPMN Execution Handlers
================================================

The runtime contains the execution logic for each BPMN element type,
split into three subpackages:

  - ``gateway/`` — Routing decisions (exclusive, parallel, inclusive, event-based)
  - ``event/``   — Event triggers and effects (start, end, intermediate, boundary)
  - ``task/``    — Task dispatch to plugin resolvers

Each handler is a standalone async function — no framework coupling.
The Temporal workflow layer calls these handlers to implement BPMN semantics.

Usage::

    from helix_engine.runtime.gateway import handle_exclusive_gateway
    from helix_engine.runtime.event import handle_start_event
    from helix_engine.runtime.task import TaskDispatcher
"""
