"""HxShield detection engine.

Evaluates inbound events against configured SecurityRules and, when a
rule fires, persists a SecurityIncident and emits a security_event onto
HxStream.  All DB writes use a separate short-lived session so the engine
never blocks the caller's request.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Pattern type constants ─────────────────────────────────────────────────────

PATTERN_DUPLICATE_FLOOD   = "duplicate_case_flood"
PATTERN_DOS_SUBMISSION    = "dos_submission"
PATTERN_VELOCITY_ANOMALY  = "velocity_anomaly"
PATTERN_OFF_HOURS_ACCESS  = "off_hours_bulk_access"
PATTERN_FIELD_ANOMALY     = "field_value_anomaly"
PATTERN_ACCOUNT_TAKEOVER  = "account_takeover"
PATTERN_INSIDER_THREAT    = "insider_threat"
PATTERN_REPLAY_ATTACK     = "replay_attack"

ALL_PATTERNS = [
    PATTERN_DUPLICATE_FLOOD,
    PATTERN_DOS_SUBMISSION,
    PATTERN_VELOCITY_ANOMALY,
    PATTERN_OFF_HOURS_ACCESS,
    PATTERN_FIELD_ANOMALY,
    PATTERN_ACCOUNT_TAKEOVER,
    PATTERN_INSIDER_THREAT,
    PATTERN_REPLAY_ATTACK,
]


# ── In-memory sliding window counters ─────────────────────────────────────────
# Keyed by (actor_id, pattern_type).  Values are lists of UTC timestamps.
_windows: dict[tuple, list[float]] = defaultdict(list)


def _window_count(actor_id: str, pattern: str, window_seconds: int) -> int:
    """Count events for actor within the rolling window."""
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - window_seconds
    key = (actor_id, pattern)
    _windows[key] = [t for t in _windows[key] if t >= cutoff]
    return len(_windows[key])


def _record_event(actor_id: str, pattern: str) -> None:
    now = datetime.now(timezone.utc).timestamp()
    _windows[(actor_id, pattern)].append(now)


def _payload_hash(context: dict) -> str:
    """Stable hash of the context payload for replay detection."""
    canonical = json.dumps(context, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:64]


# ── Rule cache ────────────────────────────────────────────────────────────────
_rule_cache: list[Any] = []
_cache_loaded_at: float = 0.0
_CACHE_TTL = 60.0  # reload rules every 60 s


async def _get_rules(session) -> list[Any]:
    global _rule_cache, _cache_loaded_at
    now = datetime.now(timezone.utc).timestamp()
    if now - _cache_loaded_at < _CACHE_TTL and _rule_cache:
        return _rule_cache
    from sqlalchemy import select
    from case_service.db.models import SecurityRuleModel
    result = await session.execute(
        select(SecurityRuleModel).where(SecurityRuleModel.enabled == True)
    )
    _rule_cache = list(result.scalars().all())
    _cache_loaded_at = now
    return _rule_cache


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_event(event_type: str, actor_id: str | None, context: dict, rules: list) -> list[Any]:
    """Return the list of rules that fire for this event."""
    fired: list[Any] = []
    actor = actor_id or "anonymous"

    for rule in rules:
        pattern = rule.pattern_type
        hit = False

        if pattern == PATTERN_DUPLICATE_FLOOD and event_type in ("case_created", "portal_submit"):
            count = _window_count(actor, pattern, rule.window_seconds)
            if count >= rule.threshold:
                hit = True

        elif pattern == PATTERN_DOS_SUBMISSION and event_type in ("case_created", "portal_submit"):
            count = _window_count(actor, pattern, rule.window_seconds)
            if count >= rule.threshold:
                hit = True

        elif pattern == PATTERN_VELOCITY_ANOMALY and event_type in ("case_closed", "case_resolved", "bulk_action"):
            count = _window_count(actor, pattern, rule.window_seconds)
            if count >= rule.threshold:
                hit = True

        elif pattern == PATTERN_OFF_HOURS_ACCESS and event_type in ("bulk_read", "export"):
            hour = datetime.now(timezone.utc).hour
            # Off-hours = 22:00-06:00 UTC
            if hour >= 22 or hour < 6:
                count = _window_count(actor, pattern, rule.window_seconds)
                if count >= rule.threshold:
                    hit = True

        elif pattern == PATTERN_FIELD_ANOMALY and event_type == "case_created":
            # Caller signals this by passing context["field_anomaly_score"] > threshold
            score = context.get("field_anomaly_score", 0)
            if score >= rule.threshold:
                hit = True

        elif pattern == PATTERN_ACCOUNT_TAKEOVER and event_type == "login":
            new_country = context.get("new_country", False)
            sensitive_access = context.get("sensitive_case_access", False)
            if new_country and sensitive_access:
                hit = True

        elif pattern == PATTERN_INSIDER_THREAT and event_type == "case_read":
            unassigned_access = context.get("unassigned_case_access", False)
            count = _window_count(actor, pattern, rule.window_seconds)
            if unassigned_access and count >= rule.threshold:
                hit = True

        elif pattern == PATTERN_REPLAY_ATTACK and event_type in ("form_submit", "portal_submit"):
            phash = _payload_hash(context)
            recent = context.get("recent_hashes", [])
            if phash in recent:
                hit = True

        if hit:
            fired.append(rule)

    return fired


# ── Main evaluation entry point ───────────────────────────────────────────────

async def evaluate(
    *,
    event_type: str,
    actor_id: str | None,
    tenant_id: str | None,
    case_type_id: str | None,
    context: dict[str, Any],
    session,
) -> dict:
    """Evaluate event against all enabled rules.

    Returns a dict with:
        score            float in [0,1]
        patterns_matched list[str]
        action           str
        incident_id      uuid | None
        explanation      str | None
    """
    actor = actor_id or "anonymous"

    rules = await _get_rules(session)
    fired = _score_event(event_type, actor, context, rules)

    # Record for sliding-window counters (after scoring to avoid self-inflation)
    for rule in rules:
        _record_event(actor, rule.pattern_type)

    if not fired:
        # Persist a low-score shield event
        await _persist_shield_event(
            session, event_type, actor_id, tenant_id, case_type_id,
            context, score=0.0, patterns_matched=[],
        )
        return {"score": 0.0, "patterns_matched": [], "action": "allow",
                "incident_id": None, "explanation": None}

    patterns_matched = [r.pattern_type for r in fired]
    # Severity → score mapping
    sev_score = {"low": 0.3, "medium": 0.6, "high": 0.85, "critical": 1.0}
    score = max(sev_score.get(r.severity, 0.5) for r in fired)

    # Highest-priority action (block > suspend > flag)
    action_order = {"flag": 0, "alert": 1, "suspend": 2, "block": 3}
    action = max(fired, key=lambda r: action_order.get(r.action, 0)).action

    explanation = _build_explanation(event_type, actor, patterns_matched, score, context)

    incident_id = await _persist_incident(
        session, fired[0], event_type, actor_id, tenant_id, case_type_id,
        context, explanation, score, patterns_matched,
    )
    await _persist_shield_event(
        session, event_type, actor_id, tenant_id, case_type_id,
        context, score, patterns_matched,
    )

    # Fire HxStream event (best-effort)
    asyncio.ensure_future(_emit_hxstream(
        event_type, actor_id, tenant_id, patterns_matched, score, action, incident_id,
    ))

    return {
        "score": score,
        "patterns_matched": patterns_matched,
        "action": action,
        "incident_id": incident_id,
        "explanation": explanation,
    }


def _build_explanation(
    event_type: str,
    actor: str,
    patterns: list[str],
    score: float,
    context: dict,
) -> str:
    parts = [f"Actor '{actor}' triggered {len(patterns)} pattern(s): {', '.join(patterns)}."]
    parts.append(f"Risk score: {score:.0%}.")
    if PATTERN_DUPLICATE_FLOOD in patterns:
        parts.append("High volume of similar case submissions detected within the time window.")
    if PATTERN_DOS_SUBMISSION in patterns:
        parts.append("Submission rate exceeds normal baseline — possible automated attack.")
    if PATTERN_VELOCITY_ANOMALY in patterns:
        parts.append("Case processing velocity is impossibly high — likely automated bulk operation.")
    if PATTERN_OFF_HOURS_ACCESS in patterns:
        parts.append("Bulk data access detected outside normal business hours.")
    if PATTERN_REPLAY_ATTACK in patterns:
        parts.append("Identical payload submitted multiple times — possible replay attack.")
    return " ".join(parts)


async def _persist_incident(
    session,
    rule,
    event_type: str,
    actor_id: str | None,
    tenant_id: str | None,
    case_type_id: str | None,
    context: dict,
    explanation: str,
    score: float,
    patterns_matched: list[str],
) -> uuid.UUID:
    from case_service.db.models import SecurityIncidentModel
    incident = SecurityIncidentModel(
        rule_id=rule.id,
        pattern_type=rule.pattern_type,
        severity=rule.severity,
        status="open",
        actor_id=actor_id,
        tenant_id=tenant_id,
        case_type_id=case_type_id,
        context={**context, "score": score, "patterns_matched": patterns_matched},
        explanation=explanation,
    )
    session.add(incident)
    await session.flush()
    return incident.id


async def _persist_shield_event(
    session,
    event_type: str,
    actor_id: str | None,
    tenant_id: str | None,
    case_type_id: str | None,
    context: dict,
    score: float,
    patterns_matched: list[str],
) -> None:
    from case_service.db.models import ShieldEventModel
    ev = ShieldEventModel(
        event_type=event_type,
        actor_id=actor_id,
        tenant_id=tenant_id,
        case_type_id=case_type_id,
        payload_hash=_payload_hash(context),
        score=score,
        patterns_matched=patterns_matched,
        raw_context=context,
    )
    session.add(ev)


async def _emit_hxstream(
    event_type: str,
    actor_id: str | None,
    tenant_id: str | None,
    patterns: list[str],
    score: float,
    action: str,
    incident_id: uuid.UUID | None,
) -> None:
    try:
        from case_service.hxstream.emitter import emit_trace
        await emit_trace(
            "security_event",
            {
                "trigger": event_type,
                "actor_id": actor_id,
                "patterns_matched": patterns,
                "score": score,
                "action": action,
                "incident_id": str(incident_id) if incident_id else None,
            },
            tenant_id=tenant_id or "default",
            actor_user_id=actor_id,
        )
    except Exception:
        logger.exception("HxStream emit failed — continuing")
