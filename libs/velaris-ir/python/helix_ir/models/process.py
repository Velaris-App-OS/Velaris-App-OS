"""
BPMN 2.0 Intermediate Representation — Process Models
======================================================

These dataclasses are the **shared vocabulary** of Helix.  Every component
— engine compiler, runtime, codegen, scout, migration — speaks this IR.

Design principles:
  1. One class per BPMN element (easy to grep, easy to extend).
  2. Plain dataclasses — no ORM, no framework, no magic.
  3. Every field has a docstring-style comment explaining its BPMN semantics.
  4. The IR is serialisation-agnostic (JSON, msgpack, protobuf — pick later).

BPMN 2.0 spec reference: https://www.omg.org/spec/BPMN/2.0/PDF

Tracing a process through the system:
  BPMN XML  →  Parser (engine/compiler/parser)
            →  IR models (this file)
            →  Validator (engine/compiler/validator)
            →  Optimizer (engine/compiler/optimizer)
            →  Temporal plan (engine/temporal)
            →  Runtime execution (engine/runtime)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


# ═══════════════════════════════════════════════════════════════════════
#  Enums — one per BPMN concept
# ═══════════════════════════════════════════════════════════════════════

class GatewayDirection(Enum):
    """Whether a gateway splits (diverging) or joins (converging) the flow."""
    DIVERGING = "diverging"
    CONVERGING = "converging"
    MIXED = "mixed"              # Both diverging and converging (rare)
    UNSPECIFIED = "unspecified"   # Modeler didn't set it — infer from edges


class MultiInstanceType(Enum):
    """How a multi-instance activity spawns its iterations."""
    NONE = auto()        # Normal single-instance activity
    PARALLEL = auto()    # All instances run at the same time
    SEQUENTIAL = auto()  # Instances run one after another


class EventType(Enum):
    """The trigger/effect attached to a BPMN event element."""
    NONE = auto()          # Plain (no definition — "just start" / "just end")
    TIMER = auto()         # ISO 8601 duration, date, or cycle
    MESSAGE = auto()       # Waiting for / sending a named message
    SIGNAL = auto()        # Broadcast signal (like an event bus)
    ERROR = auto()         # Catches or throws a named error
    ESCALATION = auto()    # Like error but non-interrupting by convention
    COMPENSATION = auto()  # Triggers compensation handlers
    CONDITIONAL = auto()   # Fires when a data condition becomes true
    TERMINATE = auto()     # Kills the entire process instance


# ═══════════════════════════════════════════════════════════════════════
#  Sequence Flow — the edges of the process graph
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class SequenceFlow:
    """
    A directed edge from one BPMN element to another.

    If ``condition`` is set, this flow is only taken when the expression
    evaluates to True (used on outgoing flows from exclusive/inclusive gateways).
    """
    id: str                              # BPMN id attribute
    source_ref: str                      # id of the source element
    target_ref: str                      # id of the target element
    name: str | None = None              # Optional human-readable label
    condition: str | None = None         # Expression string (e.g. "amount > 1000")


# ═══════════════════════════════════════════════════════════════════════
#  Event definitions — metadata for event elements
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class EventDefinition:
    """
    The trigger/effect details for an event element.

    Each event element can carry zero or more definitions.  For example,
    a boundary event might have both a timer and an error definition.
    """
    type: EventType = EventType.NONE

    # Timer specifics (ISO 8601)
    timer_value: str | None = None       # "PT30M" (duration), "2025-12-01T09:00:00" (date), "R3/PT1H" (cycle)

    # Message / signal / error refs (point to top-level definitions)
    message_ref: str | None = None
    signal_ref: str | None = None
    error_ref: str | None = None


# ═══════════════════════════════════════════════════════════════════════
#  Multi-instance configuration
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class MultiInstanceConfig:
    """
    Loop characteristics for multi-instance activities.

    Example BPMN:
      <multiInstanceLoopCharacteristics isSequential="false">
        <loopDataInputRef>order_items</loopDataInputRef>
        <completionCondition>approved_count >= 3</completionCondition>
      </multiInstanceLoopCharacteristics>
    """
    type: MultiInstanceType = MultiInstanceType.NONE
    collection: str | None = None        # Variable name resolving to an iterable
    element_variable: str | None = None  # Loop variable (like "item" in "for item in items")
    completion_condition: str | None = None  # Early-exit expression


# ═══════════════════════════════════════════════════════════════════════
#  Events — Start, End, Intermediate, Boundary
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class StartEvent:
    """
    Where a process begins.

    A process can have multiple start events (e.g. one plain, one message,
    one timer).  The engine decides which one fires based on how the
    process instance is created.
    """
    id: str
    name: str | None = None
    definitions: list[EventDefinition] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)  # SequenceFlow ids


@dataclass
class EndEvent:
    """
    Where an execution path terminates.

    A terminate end event kills all active branches.
    An error end event propagates an error to the parent scope.
    """
    id: str
    name: str | None = None
    definitions: list[EventDefinition] = field(default_factory=list)
    incoming: list[str] = field(default_factory=list)  # SequenceFlow ids


@dataclass
class IntermediateCatchEvent:
    """
    Pauses execution until a trigger fires (timer expires, message arrives, etc.).
    """
    id: str
    name: str | None = None
    definitions: list[EventDefinition] = field(default_factory=list)
    incoming: list[str] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)


@dataclass
class IntermediateThrowEvent:
    """
    Fires a trigger (sends a message, raises a signal) and continues immediately.
    """
    id: str
    name: str | None = None
    definitions: list[EventDefinition] = field(default_factory=list)
    incoming: list[str] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)


@dataclass
class BoundaryEvent:
    """
    Attached to an activity.  Fires while the activity is running.

    ``interrupting=True``  → cancels the host activity when triggered.
    ``interrupting=False`` → spawns a parallel path, host keeps running.
    """
    id: str
    attached_to: str                     # id of the host activity
    interrupting: bool = True
    name: str | None = None
    definitions: list[EventDefinition] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════
#  Tasks — the work items
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class _TaskBase:
    """Shared fields for all task types.  Not used directly."""
    id: str
    name: str | None = None
    incoming: list[str] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)
    multi_instance: MultiInstanceConfig = field(default_factory=MultiInstanceConfig)
    default_flow: str | None = None      # Default outgoing SequenceFlow id
    extensions: dict[str, Any] = field(default_factory=dict)  # helix:timeout, helix:taskQueue, etc.


@dataclass
class UserTask(_TaskBase):
    """
    A task completed by a human through a form.

    The ``form_key`` points to a form definition in the form-service.
    The engine creates a task assignment and waits for submission.
    """
    form_key: str | None = None
    assignee: str | None = None          # Expression or literal user id
    candidate_groups: list[str] = field(default_factory=list)


@dataclass
class ServiceTask(_TaskBase):
    """
    A task executed by the engine (calling an API, running a plugin, etc.).

    ``implementation`` is a URI like "helix://order-service/validate"
    that the plugin resolver uses to find the right handler.
    """
    implementation: str | None = None


@dataclass
class ScriptTask(_TaskBase):
    """
    A task that runs an inline script.

    ``language`` is "python", "javascript", "groovy", etc.
    ``script`` is the source code to execute.
    """
    language: str = "python"
    script: str = ""


@dataclass
class SendTask(_TaskBase):
    """A task that sends a message to an external participant."""
    implementation: str | None = None
    message_ref: str | None = None


@dataclass
class ReceiveTask(_TaskBase):
    """A task that waits for a message from an external participant."""
    message_ref: str | None = None


@dataclass
class ManualTask(_TaskBase):
    """A task performed outside the engine (no system interaction)."""
    pass


@dataclass
class BusinessRuleTask(_TaskBase):
    """
    A task that evaluates a decision table (DMN).

    ``decision_ref`` points to a DecisionTable in the rules-service.
    """
    decision_ref: str | None = None


@dataclass
class GenericTask(_TaskBase):
    """
    A plain <task> element — no specific type.
    Treated as a pass-through unless a plugin resolver claims it.
    """
    pass


# ═══════════════════════════════════════════════════════════════════════
#  Gateways — routing logic
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class _GatewayBase:
    """Shared fields for all gateway types.  Not used directly."""
    id: str
    name: str | None = None
    direction: GatewayDirection = GatewayDirection.UNSPECIFIED
    incoming: list[str] = field(default_factory=list)
    outgoing: list[str] = field(default_factory=list)
    default_flow: str | None = None      # Fallback SequenceFlow id


@dataclass
class ExclusiveGateway(_GatewayBase):
    """
    XOR split/join.

    Diverging: evaluates conditions on outgoing flows, takes exactly ONE.
    Converging: waits for ONE incoming flow to arrive, then continues.
    """
    pass


@dataclass
class ParallelGateway(_GatewayBase):
    """
    AND split/join.

    Diverging: activates ALL outgoing flows simultaneously.
    Converging: waits for ALL incoming flows before continuing.
    """
    pass


@dataclass
class InclusiveGateway(_GatewayBase):
    """
    OR split/join.

    Diverging: activates all outgoing flows whose conditions are true
              (at least one must be true, or the default flow is taken).
    Converging: waits for all *active* incoming flows.
    """
    pass


@dataclass
class EventBasedGateway(_GatewayBase):
    """
    Waits for one of several events to occur (first-wins).

    Each outgoing flow leads to an intermediate catch event.
    Whichever event fires first determines the path taken.
    """
    pass


# ═══════════════════════════════════════════════════════════════════════
#  Subprocess — nested process scope
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class SubProcess(_TaskBase):
    """
    An embedded subprocess — a process-within-a-process.

    Contains its own complete set of elements (start, end, tasks, gateways).
    The ``body`` field holds a nested ``BPMNProcess``.
    """
    body: BPMNProcess | None = None


@dataclass
class CallActivity(_TaskBase):
    """
    Calls a separately-defined process by reference.

    ``called_element`` is the process id of the target process.
    Unlike SubProcess, the called process is defined externally.
    """
    called_element: str | None = None


# ═══════════════════════════════════════════════════════════════════════
#  Type alias for "any BPMN element"
# ═══════════════════════════════════════════════════════════════════════

# This union makes pattern matching easy:
#   match element:
#       case UserTask(): ...
#       case ExclusiveGateway(): ...

Element = (
    StartEvent | EndEvent
    | IntermediateCatchEvent | IntermediateThrowEvent | BoundaryEvent
    | UserTask | ServiceTask | ScriptTask | SendTask | ReceiveTask
    | ManualTask | BusinessRuleTask | GenericTask
    | ExclusiveGateway | ParallelGateway | InclusiveGateway | EventBasedGateway
    | SubProcess | CallActivity
)


# ═══════════════════════════════════════════════════════════════════════
#  BPMNProcess — the top-level container
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class BPMNProcess:
    """
    A single BPMN process definition — the root of the IR.

    After parsing, every element is stored in ``elements`` keyed by its
    BPMN id, and every sequence flow in ``flows``.  The compiler phases
    (link, validate, optimize) operate on this structure.

    Quick access:
      process.elements["task_1"]                    → the element
      process.flows["flow_3"]                       → the sequence flow
      process.elements_by_type(UserTask)            → all user tasks
      process.start_events                          → entry points
    """
    id: str                              # BPMN process id attribute
    name: str | None = None              # Human-readable process name
    is_executable: bool = True           # False = documentation-only process

    elements: dict[str, Element] = field(default_factory=dict)
    flows: dict[str, SequenceFlow] = field(default_factory=dict)

    # ── Convenience accessors ─────────────────────────────────────

    @property
    def start_events(self) -> list[StartEvent]:
        """All start events in this process."""
        return [e for e in self.elements.values() if isinstance(e, StartEvent)]

    @property
    def end_events(self) -> list[EndEvent]:
        """All end events in this process."""
        return [e for e in self.elements.values() if isinstance(e, EndEvent)]

    def elements_by_type(self, cls: type) -> list[Element]:
        """Return all elements of a specific type (e.g. ``process.elements_by_type(UserTask)``)."""
        return [e for e in self.elements.values() if isinstance(e, cls)]

    def outgoing_flows(self, element_id: str) -> list[SequenceFlow]:
        """All sequence flows leaving a given element."""
        return [f for f in self.flows.values() if f.source_ref == element_id]

    def incoming_flows(self, element_id: str) -> list[SequenceFlow]:
        """All sequence flows entering a given element."""
        return [f for f in self.flows.values() if f.target_ref == element_id]

    def target_of(self, flow_id: str) -> Element | None:
        """Follow a sequence flow to its target element."""
        flow = self.flows.get(flow_id)
        return self.elements.get(flow.target_ref) if flow else None

    def successors(self, element_id: str) -> list[Element]:
        """All elements directly reachable from the given element."""
        return [
            self.elements[f.target_ref]
            for f in self.outgoing_flows(element_id)
            if f.target_ref in self.elements
        ]

    def predecessors(self, element_id: str) -> list[Element]:
        """All elements that flow into the given element."""
        return [
            self.elements[f.source_ref]
            for f in self.incoming_flows(element_id)
            if f.source_ref in self.elements
        ]
