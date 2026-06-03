"""
Event Execution Handlers
========================

Handles the runtime behaviour of BPMN events:
  - **Start events**: Process instantiation triggers.
  - **End events**: Process/branch termination effects.
  - **Intermediate catch**: Pause execution until a trigger fires.
  - **Intermediate throw**: Fire a trigger and continue.
  - **Boundary events**: Attached to activities, fire during execution.

Each handler is a standalone async function with no framework dependencies.
The Temporal workflow layer calls these to implement event semantics.

Note: Timer scheduling and message correlation are handled by the
Temporal workflow primitives.  These handlers prepare the parameters
and interpret the results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from helix_ir.models.process import (
    BoundaryEvent,
    EndEvent,
    EventDefinition,
    EventType,
    IntermediateCatchEvent,
    IntermediateThrowEvent,
    StartEvent,
)

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════════
#  Event results
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class EventResult:
    """
    The output of an event handler.

    ``action`` tells the runtime what to do next:
      - "continue"   → proceed to outgoing flows normally
      - "terminate"  → kill all active branches in this process scope
      - "error"      → propagate error to parent scope
      - "wait"       → pause execution (intermediate catch)
    """
    action: str = "continue"
    variables: dict[str, Any] | None = None   # Updated/new variables
    error_ref: str | None = None              # For error events
    signal_ref: str | None = None             # For signal events
    message_ref: str | None = None            # For message events
    timer_value: str | None = None            # ISO 8601 for timer events


# ═══════════════════════════════════════════════════════════════════════
#  Start event handler
# ═══════════════════════════════════════════════════════════════════════

async def handle_start_event(
    event: StartEvent,
    variables: dict[str, Any],
) -> EventResult:
    """
    Process a start event.

    For plain start events, this is a no-op (just continue).
    For message/signal/timer starts, log the trigger type — the actual
    triggering is handled by the process instantiation layer.
    """
    if not event.definitions:
        logger.info("start_event_plain", event_id=event.id)
        return EventResult(action="continue")

    for defn in event.definitions:
        logger.info("start_event_triggered",
                     event_id=event.id,
                     type=defn.type.name,
                     message_ref=defn.message_ref,
                     timer_value=defn.timer_value)

    return EventResult(action="continue")


# ═══════════════════════════════════════════════════════════════════════
#  End event handler
# ═══════════════════════════════════════════════════════════════════════

async def handle_end_event(
    event: EndEvent,
    variables: dict[str, Any],
) -> EventResult:
    """
    Process an end event.

    - Plain end event → "continue" (this branch ends naturally).
    - Terminate end event → "terminate" (kill everything).
    - Error end event → "error" (propagate to parent scope).
    """
    if not event.definitions:
        logger.info("end_event_plain", event_id=event.id)
        return EventResult(action="continue")

    for defn in event.definitions:
        if defn.type == EventType.TERMINATE:
            logger.info("end_event_terminate", event_id=event.id)
            return EventResult(action="terminate")

        if defn.type == EventType.ERROR:
            logger.info("end_event_error", event_id=event.id,
                         error_ref=defn.error_ref)
            return EventResult(action="error", error_ref=defn.error_ref)

        if defn.type == EventType.SIGNAL:
            logger.info("end_event_signal", event_id=event.id,
                         signal_ref=defn.signal_ref)
            return EventResult(action="continue", signal_ref=defn.signal_ref)

        if defn.type == EventType.MESSAGE:
            logger.info("end_event_message", event_id=event.id,
                         message_ref=defn.message_ref)
            return EventResult(action="continue", message_ref=defn.message_ref)

    return EventResult(action="continue")


# ═══════════════════════════════════════════════════════════════════════
#  Intermediate catch event handler
# ═══════════════════════════════════════════════════════════════════════

async def handle_intermediate_catch(
    event: IntermediateCatchEvent,
    variables: dict[str, Any],
) -> EventResult:
    """
    Pause execution until the event trigger fires.

    Returns "wait" — the Temporal layer translates this into the
    appropriate wait primitive (timer sleep, signal wait, etc.).
    """
    if not event.definitions:
        logger.warning("intermediate_catch_no_definition", event_id=event.id)
        return EventResult(action="continue")

    defn = event.definitions[0]  # Primary definition

    logger.info("intermediate_catch_waiting",
                 event_id=event.id, type=defn.type.name)

    return EventResult(
        action="wait",
        timer_value=defn.timer_value,
        message_ref=defn.message_ref,
        signal_ref=defn.signal_ref,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Intermediate throw event handler
# ═══════════════════════════════════════════════════════════════════════

async def handle_intermediate_throw(
    event: IntermediateThrowEvent,
    variables: dict[str, Any],
) -> EventResult:
    """
    Fire a trigger (send message, raise signal) and continue immediately.

    The actual message/signal dispatch is handled by the runtime's
    event bus integration.
    """
    if not event.definitions:
        logger.info("intermediate_throw_plain", event_id=event.id)
        return EventResult(action="continue")

    defn = event.definitions[0]

    logger.info("intermediate_throw_firing",
                 event_id=event.id, type=defn.type.name)

    return EventResult(
        action="continue",
        message_ref=defn.message_ref,
        signal_ref=defn.signal_ref,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Boundary event handler
# ═══════════════════════════════════════════════════════════════════════

async def handle_boundary_event(
    event: BoundaryEvent,
    variables: dict[str, Any],
) -> EventResult:
    """
    Handle a boundary event that fired on a running activity.

    The Temporal layer sets up a race between the activity and its
    boundary events.  When a boundary event wins:
      - If ``interrupting=True``: the host activity is cancelled.
      - If ``interrupting=False``: a parallel branch is spawned.

    This handler returns the event details — the cancellation logic
    lives in the Temporal workflow layer.
    """
    if not event.definitions:
        logger.warning("boundary_event_no_definition", event_id=event.id)
        return EventResult(action="continue")

    defn = event.definitions[0]

    logger.info("boundary_event_fired",
                 event_id=event.id,
                 attached_to=event.attached_to,
                 interrupting=event.interrupting,
                 type=defn.type.name)

    return EventResult(
        action="continue",
        timer_value=defn.timer_value,
        error_ref=defn.error_ref,
        message_ref=defn.message_ref,
        signal_ref=defn.signal_ref,
    )
