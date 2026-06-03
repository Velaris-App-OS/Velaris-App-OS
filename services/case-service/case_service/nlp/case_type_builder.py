"""NLP → Case Type builder.

Takes a natural language description and produces a valid
case type definition JSON that can be deployed.

Has two paths:
1. LLM-based (Ollama) — more flexible, handles arbitrary descriptions
2. Heuristic fallback — works without any LLM, recognizes common patterns

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from case_service.hxnexus.factory import generate_json as _ai_generate_json

logger = logging.getLogger(__name__)


CASE_TYPE_PROMPT = """You are an expert in business process and case management design.
Given a natural language description, produce a complete case type definition JSON.

Rules:
- Generate AS MANY stages as the process genuinely requires — do not cap at 4.
  A simple request needs 3 stages. An insurance claim needs 6-8. A loan needs 8-10.
- Every stage must have 1-4 meaningful steps reflecting real work done at that stage.
- Output ONLY valid JSON, no markdown fences, no other text.

JSON structure:
{
  "name": "<short PascalCase name>",
  "description": "<one sentence>",
  "default_priority": "low|medium|high|critical",
  "stages": [
    {
      "id": "<snake_case>",
      "name": "<display name>",
      "stage_type": "linear",
      "order": <0-based integer>,
      "sla_hours": <integer or null>,
      "steps": [
        {
          "id": "<snake_case>",
          "name": "<display name>",
          "step_type": "user_task|system_task|decision|approval|notification",
          "bpmn_element_id": "<same as id>",
          "required": true,
          "assignment": {"strategy": "queue_based|round_robin|manual"},
          "form_fields": [
            {"id": "<snake_case>", "label": "<label>", "field_type": "text|number|date|select|boolean|textarea|email|phone|file", "required": true, "options": null}
          ]
        }
      ]
    }
  ],
  "sla_policies": [
    {"name": "<name>", "target_stage": "<stage_id>", "hours": <integer>, "action": "escalate|notify|auto_close"}
  ],
  "variables": [
    {"id": "<snake_case>", "label": "<label>", "field_type": "text|number|date|select|boolean|textarea|email|phone", "required": false}
  ],
  "notifications": [
    {"trigger": "case_opened|stage_entered|case_resolved", "channel": "email|push", "recipient": "assignee|submitter|manager", "template": "<short description>"}
  ]
}

Valid step_type: user_task, system_task, decision, approval, notification.
Valid stage_type: linear, parallel.
Valid field_type: text, number, date, select, boolean, textarea, email, phone, file.

Process description:
"""

# Lightweight prompt when only the structure (no forms/SLAs) is needed
CASE_TYPE_PROMPT_SIMPLE = """You are an expert in business process modeling. Given a natural language description, output a JSON case type definition.

Output ONLY valid JSON. Generate as many stages as the process requires (do not cap at 4).

{
  "name": "<short name>",
  "description": "<one sentence>",
  "default_priority": "medium",
  "stages": [
    {
      "id": "<snake_case>",
      "name": "<display name>",
      "stage_type": "linear",
      "order": <integer>,
      "steps": [
        {
          "id": "<snake_case>",
          "name": "<display name>",
          "step_type": "user_task",
          "bpmn_element_id": "<same as id>",
          "required": true
        }
      ]
    }
  ],
  "sla_policies": []
}

Valid step_type: user_task, system_task, decision, approval, notification.

Process description:
"""


async def build_from_description(
    description: str,
    use_fallback: bool = True,
    # Legacy params kept for call-site compatibility — ignored, backend from settings
    model: str = "",
    ollama_url: str = "",
) -> dict[str, Any]:
    """Convert natural language description to case type definition (structure only).

    Uses the project-wide AI backend.  Falls back to heuristic if unavailable.
    For a full NLP Builder result with forms, SLAs, and notifications use build_full().
    """
    result = await _ai_generate_json(
        prompt=description,
        system=CASE_TYPE_PROMPT_SIMPLE,
    )

    if result and _looks_valid(result):
        return _normalize(result, source="llm")

    # Fallback: heuristic
    if use_fallback:
        logger.info("Using heuristic fallback for NLP process building")
        return _heuristic_parse(description)

    return {"error": "LLM unavailable and fallback disabled"}


async def build_full(
    description: str,
    use_fallback: bool = True,
) -> dict[str, Any]:
    """Generate a complete NLP Builder result: stages + forms + SLAs + data model + notifications.

    Full mode — produces a deployable application shell with every field,
    every form, and every policy pre-populated from the natural language description.
    """
    result = await _ai_generate_json(
        prompt=description,
        system=CASE_TYPE_PROMPT,
    )

    if result and _looks_valid(result):
        return _normalize_full(result, source="llm")

    if use_fallback:
        logger.info("Using heuristic fallback for NLP Builder full mode")
        base = _heuristic_parse(description)
        return _enrich_full(base)

    return {"error": "LLM unavailable and fallback disabled"}


def _normalize_full(result: dict[str, Any], source: str = "llm") -> dict[str, Any]:
    """Normalise a full NLP Builder result (with forms, SLAs, data model, notifications)."""
    base = _normalize(result, source=source)

    # Merge extra full-mode fields
    base["variables"] = result.get("variables", result.get("data_model", []))
    base["notifications"] = result.get("notifications", [])

    # Ensure form_fields exists on every step
    for stage in base["stages"]:
        for step in stage.get("steps", []):
            if "form_fields" not in step:
                step["form_fields"] = []
            if "assignment" not in step:
                step["assignment"] = {"strategy": "queue_based"}
            # Normalise form fields
            for f in step["form_fields"]:
                f.setdefault("required", True)
                f.setdefault("options", None)

        # Stage-level SLA in hours
        if "sla_hours" not in stage:
            stage["sla_hours"] = None

    # Enrich sla_policies if empty
    if not base["sla_policies"] and base["stages"]:
        last_stage_id = base["stages"][-1]["id"]
        base["sla_policies"] = [{
            "name": "Resolution SLA",
            "target_stage": last_stage_id,
            "hours": 72,
            "action": "escalate",
        }]

    base["_full"] = True
    return base


def _enrich_full(base: dict[str, Any]) -> dict[str, Any]:
    """Add full-mode fields to a heuristic-generated result."""
    for i, stage in enumerate(base.get("stages", [])):
        stage["sla_hours"] = (i + 1) * 24
        for step in stage.get("steps", []):
            step.setdefault("assignment", {"strategy": "queue_based"})
            step["form_fields"] = _default_form_fields(step["step_type"], stage["id"])

    base.setdefault("variables", [
        {"id": "subject", "label": "Subject", "field_type": "text", "required": True},
        {"id": "description", "label": "Description", "field_type": "textarea", "required": False},
        {"id": "priority", "label": "Priority", "field_type": "select", "required": False,
         "options": ["low", "medium", "high", "critical"]},
    ])
    base.setdefault("notifications", [
        {"trigger": "case_opened",   "channel": "email", "recipient": "submitter",  "template": "Case opened confirmation"},
        {"trigger": "stage_entered", "channel": "push",  "recipient": "assignee",   "template": "New task assigned"},
        {"trigger": "case_resolved", "channel": "email", "recipient": "submitter",  "template": "Case resolved notification"},
    ])
    base["_full"] = True
    return base


def _default_form_fields(step_type: str, stage_id: str) -> list[dict]:
    """Generate sensible default form fields for a given step type."""
    if step_type == "approval":
        return [
            {"id": "decision", "label": "Decision", "field_type": "select",
             "required": True, "options": ["approved", "rejected", "deferred"]},
            {"id": "comments", "label": "Comments", "field_type": "textarea", "required": False},
        ]
    if step_type == "notification":
        return [
            {"id": "message", "label": "Message", "field_type": "textarea", "required": True},
        ]
    if step_type == "decision":
        return [
            {"id": "outcome", "label": "Outcome", "field_type": "select",
             "required": True, "options": ["yes", "no", "escalate"]},
            {"id": "rationale", "label": "Rationale", "field_type": "textarea", "required": False},
        ]
    # user_task / system_task default
    return [
        {"id": f"{stage_id}_notes", "label": "Notes", "field_type": "textarea", "required": False},
        {"id": f"{stage_id}_completed_by", "label": "Completed By", "field_type": "text", "required": False},
    ]


def _looks_valid(result: dict) -> bool:
    """Basic validation — does the result look like a case type?"""
    if not isinstance(result, dict):
        return False
    if "stages" not in result or not isinstance(result["stages"], list):
        return False
    return True


def _normalize(result: dict[str, Any], source: str = "llm") -> dict[str, Any]:
    """Ensure all required fields are present and structure is clean."""
    name = result.get("name", "Generated Process")
    description = result.get("description", "")

    stages = []
    for i, stage in enumerate(result.get("stages", [])):
        stage_id = stage.get("id") or _slug(stage.get("name", f"stage_{i+1}"))
        steps = []
        for j, step in enumerate(stage.get("steps", [])):
            step_id = step.get("id") or _slug(step.get("name", f"step_{j+1}"))
            steps.append({
                "id": step_id,
                "name": step.get("name", step_id.replace("_", " ").title()),
                "step_type": step.get("step_type", "user_task"),
                "bpmn_element_id": step.get("bpmn_element_id", step_id),
                "required": step.get("required", True),
                "assignment": step.get("assignment", {"strategy": "queue_based"}),
            })
        stages.append({
            "id": stage_id,
            "name": stage.get("name", stage_id.replace("_", " ").title()),
            "stage_type": stage.get("stage_type", "linear"),
            "order": stage.get("order", i),
            "steps": steps,
        })

    return {
        "name": name,
        "description": description,
        "default_priority": result.get("default_priority", "medium"),
        "stages": stages,
        "sla_policies": result.get("sla_policies", []),
        "_source": source,
    }


def _slug(text: str) -> str:
    """Convert text to snake_case identifier."""
    text = re.sub(r"[^a-zA-Z0-9\s]", "", text)
    return re.sub(r"\s+", "_", text.strip()).lower() or "item"


# ─── Heuristic fallback ─────────────────────────────────────────

# Common process patterns
PATTERN_KEYWORDS = {
    "approval": {
        "stages": ["submission", "review", "approval", "notification"],
        "priority": "medium",
    },
    "onboard": {
        "stages": ["application", "verification", "setup", "welcome"],
        "priority": "medium",
    },
    "claim": {
        "stages": ["intake", "document_collection", "verification", "assessment", "decision", "resolution", "payment", "closure"],
        "priority": "high",
    },
    "complaint": {
        "stages": ["intake", "investigation", "resolution", "follow_up"],
        "priority": "high",
    },
    "request": {
        "stages": ["submission", "review", "fulfillment"],
        "priority": "medium",
    },
    "order": {
        "stages": ["received", "processing", "fulfillment", "shipped", "delivered"],
        "priority": "medium",
    },
    "ticket": {
        "stages": ["open", "in_progress", "resolved", "closed"],
        "priority": "medium",
    },
    "loan": {
        "stages": ["application", "document_collection", "credit_check", "underwriting", "risk_assessment", "decision", "agreement", "disbursement"],
        "priority": "high",
    },
    "hire": {
        "stages": ["applied", "screening", "phone_interview", "technical_interview", "panel_interview", "offer", "background_check", "onboarding"],
        "priority": "medium",
    },
    "invoice": {
        "stages": ["received", "verification", "approval", "payment"],
        "priority": "medium",
    },
}

STEP_KEYWORDS = {
    "review": "user_task",
    "approve": "approval",
    "check": "user_task",
    "notify": "notification",
    "email": "notification",
    "verify": "user_task",
    "validate": "user_task",
    "calculate": "system_task",
    "assign": "user_task",
    "decide": "decision",
    "decision": "decision",
    "process": "system_task",
    "send": "notification",
}


def _heuristic_parse(description: str) -> dict[str, Any]:
    """Heuristic process builder — no LLM required."""
    desc_lower = description.lower()

    # Extract name (first 5 words or less)
    words = description.split()[:5]
    name = " ".join(words).strip(".,!?").title() if words else "New Process"

    # Detect pattern
    matched_pattern = None
    for keyword, pattern in PATTERN_KEYWORDS.items():
        if keyword in desc_lower:
            matched_pattern = pattern
            break

    # Build stages
    if matched_pattern:
        stage_names = matched_pattern["stages"]
        priority = matched_pattern["priority"]
    else:
        # Generic fallback
        stage_names = _extract_stage_hints(description) or ["intake", "review", "resolution"]
        priority = "medium"

    stages = []
    for i, stage_name in enumerate(stage_names):
        stage_id = _slug(stage_name)
        # Each stage gets one generic step
        step_type = _detect_step_type(stage_name)
        steps = [{
            "id": f"{stage_id}_task",
            "name": f"{stage_name.replace('_', ' ').title()} Task",
            "step_type": step_type,
            "bpmn_element_id": f"{stage_id}_task",
            "required": True,
            "assignment": {"strategy": "queue_based"},
        }]

        stages.append({
            "id": stage_id,
            "name": stage_name.replace("_", " ").title(),
            "stage_type": "linear",
            "order": i,
            "steps": steps,
        })

    return {
        "name": name,
        "description": description[:200],
        "default_priority": priority,
        "stages": stages,
        "sla_policies": [],
        "_source": "heuristic",
    }


def _extract_stage_hints(description: str) -> list[str]:
    """Look for numbered/bulleted lists or 'then' sequences."""
    # Numbered list
    numbered = re.findall(r"(?:^|\n)\s*\d+[\.\)]\s*([^\n]+)", description, re.MULTILINE)
    if numbered:
        return [_slug(n)[:30] for n in numbered[:10]]

    # "First... then... finally..." pattern
    sequence_words = ["first", "then", "next", "after", "finally", "lastly"]
    if any(w in description.lower() for w in sequence_words):
        sentences = re.split(r"[.!?]\s+", description)
        stages = []
        for s in sentences:
            for w in sequence_words:
                if s.lower().startswith(w):
                    clean = re.sub(rf"^{w}\s*,?\s*", "", s, flags=re.IGNORECASE).strip()
                    if clean:
                        stages.append(_slug(clean)[:30])
        if stages:
            return stages

    return []


def _detect_step_type(name: str) -> str:
    """Guess step type based on stage/step name."""
    name_lower = name.lower()
    for keyword, step_type in STEP_KEYWORDS.items():
        if keyword in name_lower:
            return step_type
    return "user_task"
