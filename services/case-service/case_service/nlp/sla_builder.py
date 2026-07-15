"""HxDraft P2 — natural language → a DRAFT SLA policy, validated before the card.

SLA policies are not standalone artifacts: the runtime shape consumed by
case_lifecycle/sla_tracker lives inside the case-type ``definition_json.sla_policies``.
An SLA draft is therefore a *scoped diff on a case type* — Apply re-fetches the case
type, checks the base checksum (never a blind overwrite) and appends the policy.

Gate (design §10.2):
  * both durations must parse as ISO-8601 and be > 0
  * goal ≤ deadline
  * scope ∈ {case, stage}; stage scope requires a stage that exists in the definition
  * policy id unique within the case type; ≤ 20 policies per case type
  * schema-strict normalization — unknown keys never survive

AI-down posture (signed off 2026-07-05): a **deterministic duration parser**
fallback, clearly labelled ``source: "fallback"`` — it only uses durations written
explicitly in the prompt and still fails honestly when none are parseable.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
from typing import Any

from case_service.core.sla_tracker import parse_iso8601_duration
from case_service.hxnexus.factory import generate_json as _ai_generate_json
from case_service.nlp.case_type_builder import _slug

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000
MAX_POLICIES_PER_CASE_TYPE = 20

SLA_PROMPT = """You are an expert in service-level agreements for business processes.
Convert the user's description into ONE SLA policy as JSON. Output ONLY valid JSON,
no markdown fences, no commentary.

JSON structure (use EXACTLY these keys):
{
  "name": "<short descriptive name>",
  "description": "<one sentence>",
  "scope": "case" or "stage",
  "target_stage": "<stage id when scope is stage, else null>",
  "goal_duration": "<ISO 8601 duration, e.g. PT24H>",
  "deadline_duration": "<ISO 8601 duration, e.g. PT48H>"
}
The goal is the target time; the deadline is the breach time. Goal must not exceed
the deadline. If only one time is described, use it for both."""


class SLADraftError(Exception):
    """Draft generation failed in a way the card should state honestly."""


# ── deterministic duration parsing (fallback + LLM-output tolerance) ────────────

_HUMAN_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|m\b|hours?|hrs?|h\b|days?|d\b|weeks?|w\b)",
    re.IGNORECASE,
)

_UNIT_TO_MINUTES = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}


def _minutes_to_iso(minutes: int) -> str:
    days, rem = divmod(int(minutes), 60 * 24)
    hours, mins = divmod(rem, 60)
    time_part = (f"{hours}H" if hours else "") + (f"{mins}M" if mins else "")
    if days and time_part:
        return f"P{days}DT{time_part}"
    if days:
        return f"P{days}D"
    return f"PT{time_part or '0M'}"


def to_iso8601(value: Any) -> str | None:
    """Accept an ISO-8601 duration or a human one ('48h', '2 days') → ISO-8601.

    Returns None when the value cannot be parsed deterministically.
    """
    s = str(value or "").strip()
    if not s:
        return None
    try:
        parse_iso8601_duration(s)
        return s.upper()
    except ValueError:
        pass
    m = _HUMAN_DURATION_RE.fullmatch(s)
    if not m:
        return None
    qty = float(m.group(1))
    unit = m.group(2)[0].lower()
    return _minutes_to_iso(round(qty * _UNIT_TO_MINUTES[unit]))


def _fallback_parse(description: str) -> dict[str, Any]:
    """AI-down fallback: use ONLY durations written explicitly in the prompt.

    'goal 24h … 48h' → goal PT24H, deadline PT48H (smaller = goal). A single
    duration is used for both. No durations → honest error, never a guess.
    """
    found = [(round(float(q) * _UNIT_TO_MINUTES[u[0].lower()]))
             for q, u in _HUMAN_DURATION_RE.findall(description)]
    if not found:
        raise SLADraftError(
            "AI backend unavailable and no explicit durations (like '48h' or "
            "'2 days') found in the description — SLA drafts are never guessed")
    goal, deadline = min(found), max(found)
    return {
        "name": "Drafted SLA",
        "description": "",
        "scope": "case",
        "target_stage": None,
        "goal_duration": _minutes_to_iso(goal),
        "deadline_duration": _minutes_to_iso(deadline),
    }


# ── validation gate ─────────────────────────────────────────────────────────────

def validate_sla_draft(draft: dict[str, Any],
                       case_type_definition: dict[str, Any]) -> list[str]:
    """Every reason this draft must not become a card. Empty list = renderable."""
    errors: list[str] = []
    if not isinstance(draft, dict):
        return ["Draft is not an object"]
    policy = draft.get("policy")
    if not isinstance(policy, dict):
        return ["Draft has no policy"]

    if not str(policy.get("name") or "").strip():
        errors.append("SLA policy needs a name")

    durations: dict[str, Any] = {}
    for key in ("goal_duration", "deadline_duration"):
        raw = policy.get(key)
        try:
            td = parse_iso8601_duration(str(raw or ""))
        except ValueError:
            errors.append(f"{key} {raw!r} is not a valid ISO 8601 duration")
            continue
        if td.total_seconds() <= 0:
            errors.append(f"{key} must be greater than zero")
        durations[key] = td
    if len(durations) == 2 and durations["goal_duration"] > durations["deadline_duration"]:
        errors.append("goal_duration must not exceed deadline_duration")

    scope = policy.get("scope")
    if scope not in ("case", "stage"):
        errors.append(f"scope must be 'case' or 'stage' (got {scope!r})")
    stages = {s.get("id") for s in (case_type_definition or {}).get("stages", [])
              if isinstance(s, dict)}
    if scope == "stage":
        if not policy.get("target_stage"):
            errors.append("stage-scoped SLA needs a target_stage")
        elif policy["target_stage"] not in stages:
            errors.append(f"target_stage {policy['target_stage']!r} does not exist "
                          f"on this case type")

    existing = [p for p in (case_type_definition or {}).get("sla_policies", [])
                if isinstance(p, dict)]
    if len(existing) >= MAX_POLICIES_PER_CASE_TYPE:
        errors.append(f"Case type already has {len(existing)} SLA policies "
                      f"(max {MAX_POLICIES_PER_CASE_TYPE})")
    if policy.get("id") and any(p.get("id") == policy["id"] for p in existing):
        errors.append(f"An SLA policy with id {policy['id']!r} already exists "
                      f"on this case type")

    return errors


# ── normalization (schema-strict: unknown keys never survive) ───────────────────

def normalize_sla_draft(raw: dict[str, Any], *, prompt: str = "") -> dict[str, Any]:
    name = str(raw.get("name") or "Drafted SLA").strip()[:255]
    provenance = (f'Drafted by HxNexus — "{prompt.strip()[:300]}"'
                  if prompt else "Drafted by HxNexus")
    description = str(raw.get("description") or "").strip()[:500]
    scope = str(raw.get("scope") or "case").strip().lower()
    target_stage = raw.get("target_stage")
    policy: dict[str, Any] = {
        "id": _slug(name),
        "name": name,
        "scope": scope,
        "goal_duration": to_iso8601(raw.get("goal_duration"))
        or str(raw.get("goal_duration") or ""),
        "deadline_duration": to_iso8601(raw.get("deadline_duration"))
        or str(raw.get("deadline_duration") or ""),
        "description": f"{description} · {provenance}" if description else provenance,
    }
    if scope == "stage" and target_stage:
        policy["target_stage"] = str(target_stage)[:255]
    return policy


# ── generation ──────────────────────────────────────────────────────────────────

async def generate_sla_draft(description: str,
                             case_type_definition: dict[str, Any]
                             ) -> dict[str, Any]:
    """NL → normalized SLA policy + validation errors, labelled by source.

    Returns ``{"source": "llm"|"fallback", "draft": {"policy": {...}},
    "errors": [...]}``.
    """
    description = (description or "").strip()
    if not description:
        raise SLADraftError("Describe the SLA you want drafted")
    if len(description) > MAX_PROMPT_CHARS:
        raise SLADraftError(f"Description too long (max {MAX_PROMPT_CHARS} characters)")

    raw = await _ai_generate_json(prompt=description, system=SLA_PROMPT)
    source = "llm"
    if not raw or not isinstance(raw, dict):
        raw = _fallback_parse(description)  # deterministic, or an honest error
        source = "fallback"
        logger.info("sla_builder: AI unavailable — deterministic fallback used")

    draft = {"policy": normalize_sla_draft(raw, prompt=description)}
    return {"source": source, "draft": draft,
            "errors": validate_sla_draft(draft, case_type_definition)}
