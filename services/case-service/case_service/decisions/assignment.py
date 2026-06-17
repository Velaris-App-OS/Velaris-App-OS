"""Assignment routing — the first concrete DecisionPoint (#22 Phase 1).

PolicyResolver wraps the existing 8-strategy assignment router verbatim —
zero behavior change when AI is off (the default). The AI resolver asks the
configured HxNexus backend to pick an assignee, with a strict JSON contract;
anything malformed is confidence 0.0 and policy wins.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .service import DecisionPoint

log = logging.getLogger(__name__)


class AssignmentPolicyResolver:
    name = "assignment-rules"

    async def resolve(self, session: AsyncSession, context: dict[str, Any]) -> Any:
        from case_service.core.assignment_router import resolve_assignment
        assignee_type, assignee_id = await resolve_assignment(
            session, context.get("rule"), context.get("context"),
        )
        return {"assignee_type": assignee_type, "assignee_id": assignee_id}


class AssignmentAIResolver:
    """HxNexus-backed assignee suggestion. Output contract:
    {"assignee_type": "user|role|queue", "assignee_id": "...", "confidence": 0.0-1.0}
    Any deviation → (None, 0.0) → policy decides."""
    name = "assignment-hxnexus"

    async def resolve(
        self, session: AsyncSession, context: dict[str, Any],
    ) -> tuple[Any, float]:
        from case_service.hxnexus.factory import generate_json

        rule = context.get("rule") or {}
        case_ctx = context.get("context") or {}
        prompt = (
            "You route a work item to an assignee.\n"
            f"Assignment rule: {json.dumps(rule, default=str)[:800]}\n"
            f"Case context: {json.dumps(case_ctx, default=str)[:1500]}\n"
            'Respond with: {"assignee_type": "user"|"role"|"queue", '
            '"assignee_id": "<id>", "confidence": <0.0-1.0>}'
        )
        parsed = await generate_json(prompt, temperature=0.1, max_tokens=200)
        if not isinstance(parsed, dict):
            return None, 0.0
        try:
            assignee_type = parsed["assignee_type"]
            assignee_id = str(parsed["assignee_id"])
            confidence = float(parsed.get("confidence", 0.0))
        except (KeyError, TypeError, ValueError):
            return None, 0.0
        if assignee_type not in ("user", "role", "queue") or not assignee_id:
            return None, 0.0
        if len(assignee_id) > 255:
            return None, 0.0
        # Case context is partially caller-controlled (portal subjects etc.)
        # — classic prompt-injection surface (§5.3). A "user" pick must at
        # least be a REAL operator; fabricated ids die here. Routing to an
        # existing colluding operator remains the documented residual,
        # bounded by per-case-type opt-in + the audited policy_alternative.
        if assignee_type == "user":
            from sqlalchemy import select
            from case_service.db.models import UserDirectoryModel
            exists = (await session.execute(
                select(UserDirectoryModel.id)
                .where(UserDirectoryModel.user_id == assignee_id).limit(1)
            )).scalar_one_or_none()
            if exists is None:
                log.warning("assignment AI proposed unknown user %r — discarded", assignee_id)
                return None, 0.0
        return {"assignee_type": assignee_type, "assignee_id": assignee_id}, confidence


assignment_decision_point = DecisionPoint(
    name="assignment_routing",
    policy_resolver=AssignmentPolicyResolver(),
    ai_resolver=AssignmentAIResolver(),
)
