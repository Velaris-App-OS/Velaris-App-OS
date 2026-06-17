"""Group H (AI Egress Guard layer 7) — tamper-evident audit of AI egress.

Every completion that leaves the platform for an external AI provider is
recorded as a SecurityEventModel row (event_type="ai.egress"), which makes
it visible in HxShield and available for anomaly detection (runaway egress
volume = compromised key or injection attack).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def chunk_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:12]


async def record_egress(
    session: AsyncSession,
    *,
    user_id: str | None,
    purpose: str,                     # "doc_qa" | "chat" | ...
    provider: str,
    case_id=None,
    doc_ids: list[str] | None = None,
    chunk_hashes: list[str] | None = None,
    bytes_out: int = 0,
    pseudonymized: bool = False,
    redactions: int = 0,
) -> None:
    """Append an ai.egress security event. Caller owns the commit."""
    from case_service.db.models import SecurityEventModel

    try:
        session.add(SecurityEventModel(
            event_type="ai.egress",
            severity="info",
            user_id=str(user_id) if user_id else None,
            resource_type="case" if case_id else "conversation",
            resource_id=str(case_id) if case_id else None,
            action=purpose,
            outcome="sent",
            details={
                "provider": provider,
                "doc_ids": doc_ids or [],
                "chunk_hashes": chunk_hashes or [],
                "bytes_out": bytes_out,
                "pseudonymized": pseudonymized,
                "redactions": redactions,
            },
        ))
        logger.info(
            "ai.egress: purpose=%s provider=%s bytes=%d pseudonymized=%s redactions=%d",
            purpose, provider, bytes_out, pseudonymized, redactions,
        )
    except Exception as exc:
        # Auditing must never break the answer path — but always leave a trace
        logger.warning("ai.egress audit failed: %s", exc)


# ── Generic egress queue ──────────────────────────────────────────────────────
# qa_over_documents/chat write rich audit rows themselves. Every OTHER caller
# of llm.complete() (generate_json → NLP Builder, Scout, BPM importer, …)
# reaches the external provider through the EgressGuardedBackend ladder; the
# wrapper queues a generic event here and the PlatformUpdateWatcher cycle
# flushes the queue into SecurityEvents — no external completion goes
# unrecorded.
from collections import deque  # noqa: E402

_PENDING: deque = deque(maxlen=1000)


def queue_egress(*, purpose: str, provider: str, bytes_out: int) -> None:
    """Sync, sessionless — callable from inside the backend wrapper."""
    from datetime import datetime, timezone
    _PENDING.append({
        "purpose": purpose,
        "provider": provider,
        "bytes_out": bytes_out,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("ai.egress (queued): purpose=%s provider=%s bytes=%d", purpose, provider, bytes_out)


# ── Prompt-injection signal queue (§5.3) ──────────────────────────────────────
# The universal guard (factory.GuardedBackend) scans every prompt for injection
# signals and queues a flagged event here; the same watcher cycle flushes it to
# an ai.prompt_flagged SecurityEvent — a HxShield anomaly signal (§5.4).
_FLAGGED: deque = deque(maxlen=1000)


def queue_prompt_flagged(*, signals: list[str], purpose: str = "completion") -> None:
    """Sync, sessionless — record a flagged (suspicious) prompt for audit."""
    from datetime import datetime, timezone
    _FLAGGED.append({
        "signals": sorted(set(signals)),
        "purpose": purpose,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    logger.warning("ai.prompt_flagged (queued): signals=%s purpose=%s", signals, purpose)


async def flush_pending(session: AsyncSession) -> int:
    """Drain queued egress + prompt-flagged events into SecurityEvents. Caller commits."""
    from case_service.db.models import SecurityEventModel

    flushed = 0
    while _PENDING:
        ev = _PENDING.popleft()
        session.add(SecurityEventModel(
            event_type="ai.egress",
            severity="info",
            resource_type="completion",
            action=ev["purpose"],
            outcome="sent",
            details={"provider": ev["provider"], "bytes_out": ev["bytes_out"],
                     "queued_at": ev["at"], "pseudonymized": False},
        ))
        flushed += 1
    while _FLAGGED:
        ev = _FLAGGED.popleft()
        session.add(SecurityEventModel(
            event_type="ai.prompt_flagged",
            severity="warning",
            resource_type="completion",
            action=ev["purpose"],
            outcome="flagged",
            details={"signals": ev["signals"], "queued_at": ev["at"]},
        ))
        flushed += 1
    return flushed
