"""DecisionPoint — every automated decision flows through one abstraction.

Resolution rule (report §4.2, verbatim):
    If AI confidence >= threshold:  use AI decision
    Else:                           use policy decision
    If AI unavailable / timeout:    use policy decision (no outage)

Invariants:
  - The PolicyResolver ALWAYS runs — its output is either the decision or
    the recorded `policy_alternative` of an AI decision (accountability:
    every AI decision can be explained by what policy would have done; this
    is also the GDPR Art. 22 record shape, Tier-3 ready).
  - Any AIResolver exception or timeout degrades silently to policy. An AI
    outage is never a platform outage.
  - AI runs only when: an ai_resolver is wired AND the case type opts in
    (decision_ai_enabled in definition_json) AND the step's cognitive mode
    is not "manual".
  - Every resolution is appended to the case audit trail
    (action="decision_point") — source, confidence, policy alternative.

Phase 1 wires cognitive modes "manual" and "automatic" (§4.3). "assisted"
and "autonomous" need operator-confirmation UI / override windows — they
are Phase 2 and are treated as "manual" until then (fail-safe: no AI
action without the machinery to review it).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

AI_TIMEOUT_SECONDS = 3.0
DEFAULT_THRESHOLD = 0.8

#: Phase-2 modes deliberately collapse to manual until their review
#: machinery exists.
_PHASE2_MODES = {"assisted", "autonomous"}


@dataclass(frozen=True)
class DecisionOutcome:
    decision: Any                       # resolver-specific payload
    source: str                         # "policy" | "ai"
    confidence: float                   # 1.0 for policy (deterministic)
    policy_alternative: Any | None      # what policy would have done (ai only)
    reason: str


class PolicyResolver(Protocol):
    name: str
    async def resolve(self, session: AsyncSession, context: dict[str, Any]) -> Any: ...


class AIResolver(Protocol):
    name: str
    async def resolve(
        self, session: AsyncSession, context: dict[str, Any],
    ) -> tuple[Any, float]: ...          # (decision, confidence 0.0-1.0)


def decision_ai_config(case_type_definition: dict | None) -> tuple[bool, float]:
    """(ai_enabled, threshold) from a case type's definition_json.
    AI is opt-in per case type (user-approved default OFF)."""
    d = case_type_definition or {}
    enabled = bool(d.get("decision_ai_enabled", False))
    try:
        threshold = float(d.get("decision_ai_threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    return enabled, min(max(threshold, 0.0), 1.0)


def cognitive_mode_for_step(step: dict | None) -> str:
    """§4.3 cognitive mode, per step. Unknown/Phase-2 modes → manual."""
    mode = str((step or {}).get("cognitive_mode", "manual")).lower()
    if mode in _PHASE2_MODES or mode not in ("manual", "automatic"):
        return "manual"
    return mode


class DecisionPoint:
    def __init__(
        self, name: str,
        policy_resolver: PolicyResolver,
        ai_resolver: AIResolver | None = None,
    ):
        self.name = name
        self.policy = policy_resolver
        self.ai = ai_resolver

    async def resolve(
        self,
        session: AsyncSession,
        context: dict[str, Any],
        *,
        case_id: uuid.UUID | None = None,
        ai_enabled: bool = False,
        threshold: float = DEFAULT_THRESHOLD,
        cognitive_mode: str = "manual",
    ) -> DecisionOutcome:
        policy_decision = await self.policy.resolve(session, context)

        outcome = DecisionOutcome(
            decision=policy_decision, source="policy", confidence=1.0,
            policy_alternative=None, reason=f"policy:{self.policy.name}",
        )

        if self.ai is not None and ai_enabled and cognitive_mode == "automatic":
            try:
                ai_decision, confidence = await asyncio.wait_for(
                    self.ai.resolve(session, context), timeout=AI_TIMEOUT_SECONDS,
                )
                confidence = min(max(float(confidence), 0.0), 1.0)
                if ai_decision is not None and confidence >= threshold:
                    outcome = DecisionOutcome(
                        decision=ai_decision, source="ai", confidence=confidence,
                        policy_alternative=policy_decision,
                        reason=f"ai:{self.ai.name} conf={confidence:.2f} >= {threshold:.2f}",
                    )
                else:
                    outcome = DecisionOutcome(
                        decision=policy_decision, source="policy", confidence=1.0,
                        policy_alternative=None,
                        reason=(f"policy:{self.policy.name} "
                                f"(ai conf={confidence:.2f} < {threshold:.2f})"),
                    )
            except Exception as exc:   # timeout, provider down, bad output —
                outcome = DecisionOutcome(   # never an outage (§4.2)
                    decision=policy_decision, source="policy", confidence=1.0,
                    policy_alternative=None,
                    reason=f"policy:{self.policy.name} (ai unavailable: {type(exc).__name__})",
                )

        if case_id is not None:
            await self._audit(session, case_id, outcome)
        return outcome

    async def _audit(self, session: AsyncSession, case_id: uuid.UUID,
                     outcome: DecisionOutcome) -> None:
        """Decision trail on the case audit log (hash-chained, Timeline-visible).
        Best-effort: an audit failure must not lose the decision."""
        try:
            from case_service.db import repository as repo
            await repo.append_audit_entry(session, data={
                "case_id": case_id,
                "action": "decision_point",
                "actor_type": "system",
                "details": {
                    "decision_point": self.name,
                    "source": outcome.source,
                    "confidence": outcome.confidence,
                    "decision": _jsonable(outcome.decision),
                    "policy_alternative": _jsonable(outcome.policy_alternative),
                    "reason": outcome.reason,
                },
            })
        except Exception as exc:
            log.warning("decision_point audit failed (%s): %s", self.name, exc)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)
