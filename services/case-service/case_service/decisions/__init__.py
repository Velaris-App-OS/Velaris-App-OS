"""DecisionPoint abstraction — the Two-Path Architecture (report §4.2/§4.3, roadmap #22).

The platform is intelligent structurally (policies, rules, SLA, workload);
AI augments when configured. AI is never a single point of failure.
"""
from .service import (
    AIResolver,
    DecisionOutcome,
    DecisionPoint,
    PolicyResolver,
    cognitive_mode_for_step,
    decision_ai_config,
)

__all__ = [
    "AIResolver", "DecisionOutcome", "DecisionPoint", "PolicyResolver",
    "cognitive_mode_for_step", "decision_ai_config",
]
