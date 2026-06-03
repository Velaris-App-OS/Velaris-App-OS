"""HxFusion AI Director — HxNexus-powered stage automation advisor.

At any stage boundary HxNexus evaluates whether the current step can be
automated via a BPMN process.  Returns a structured recommendation the
operator can accept or override.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

_SYSTEM = """\
You are HxFusion Director, an AI that decides whether a case processing step
can be automated via a BPMN process.

Respond ONLY with valid JSON matching this schema:
{
  "can_automate": boolean,
  "confidence": float (0.0–1.0),
  "suggestion": "one-sentence recommendation",
  "reasoning": "2–3 sentence explanation"
}
No markdown, no extra text.
"""


async def advise(
    *,
    case_id: uuid.UUID,
    stage_id: str,
    case_type_id: str | None,
    context: dict[str, Any],
    available_definitions: list[dict],
    session,
) -> dict:
    """Ask HxNexus whether this stage can be automated.

    Returns a dict with keys: can_automate, confidence, suggestion,
    recommended_definition_id, reasoning.
    """
    from case_service.hxnexus.factory import get_llm_backend

    llm = get_llm_backend()

    defn_summary = "\n".join(
        f"- {d['id']}: {d['name']} (case_type={d.get('case_type_id', 'any')})"
        for d in available_definitions[:10]
    )

    prompt = (
        f"Case ID: {case_id}\n"
        f"Stage: {stage_id}\n"
        f"Case type: {case_type_id or 'unknown'}\n"
        f"Context keys: {', '.join(context.keys()) or 'none'}\n\n"
        f"Available process definitions:\n{defn_summary or 'none'}\n\n"
        "Should this stage be automated via a BPMN process? "
        "If yes, recommend the best definition ID from the list above (or null if none fits)."
    )

    try:
        raw = await llm.complete(prompt, system=_SYSTEM, temperature=0.2)
        data = json.loads(raw)
    except Exception:
        data = {
            "can_automate": False,
            "confidence": 0.0,
            "suggestion": "AI Director unavailable — defaulting to manual mode.",
            "reasoning": "Could not reach HxNexus backend.",
        }

    # Match recommended definition by name or ID
    recommended_id: uuid.UUID | None = None
    raw_rec = data.get("recommended_definition_id") or data.get("recommendation")
    if raw_rec and available_definitions:
        for d in available_definitions:
            if str(d["id"]) == str(raw_rec) or d["name"] == str(raw_rec):
                try:
                    recommended_id = uuid.UUID(str(d["id"]))
                except ValueError:
                    pass
                break

    return {
        "can_automate": bool(data.get("can_automate", False)),
        "confidence": float(data.get("confidence", 0.0)),
        "suggestion": str(data.get("suggestion", "")),
        "recommended_definition_id": recommended_id,
        "reasoning": str(data.get("reasoning", "")),
    }
