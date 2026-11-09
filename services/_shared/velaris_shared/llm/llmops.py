"""LLMOps observability — per-call metrics, cost and quality (roadmap #32, §5.4).

Every completion the platform makes is recorded once: which model answered,
how big the exchange was, what it cost, how long it took, and whether it
failed. Without this there is no way to answer "what is AI costing us", "which
caller is burning the budget", or "did quality change after we switched
models" — the three questions §5.4 exists to make answerable.

**Sessionless by necessity.** The recording point is
`factory.GuardedBackend.complete`, the one chokepoint every AI call funnels
through, and it has no `AsyncSession` — it sits below the request layer. So
calls are queued in memory and drained by the same watcher cycle that already
flushes the egress audit. A dropped metric must never cost a completion.

**Token counts are estimates unless the provider says otherwise.** Ollama and
the HTTP backends here do not return usage, so `tokens_estimated=True` and the
count is chars/4. That is honest and roughly right for English; a cost figure
built on it is an order-of-magnitude guide, not an invoice. Rates live in
:data:`MODEL_RATES` and are per **million** tokens.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Bounded so a watcher outage cannot grow without limit — metrics are
#: droppable, completions are not.
_PENDING: deque = deque(maxlen=5000)

#: Rough characters per token for English. Used only when the provider
#: reports no usage, which is every backend here today.
_CHARS_PER_TOKEN = 4.0

#: USD per **million** tokens, (input, output). Unknown models cost 0.0 and
#: are reported as `rate_known=False` rather than silently priced at zero.
MODEL_RATES: dict[str, tuple[float, float]] = {
    # Local models are free to run; the row still exists so volume is visible.
    "llama3.2": (0.0, 0.0),
    "llama3": (0.0, 0.0),
    "mistral": (0.0, 0.0),
    "phi3": (0.0, 0.0),
}


def estimate_tokens_from_chars(chars: int) -> int:
    """Character-length estimate — no backend here reports provider usage."""
    if chars <= 0:
        return 0
    return max(1, int(chars / _CHARS_PER_TOKEN))


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, bool]:
    """(cost, rate_known). An unpriced model reports 0.0 and rate_known=False."""
    rates = MODEL_RATES.get((model or "").lower())
    if rates is None:
        return 0.0, False
    in_rate, out_rate = rates
    cost = (prompt_tokens / 1_000_000.0) * in_rate + (completion_tokens / 1_000_000.0) * out_rate
    return round(cost, 8), True


def queue_call(
    *,
    provider: str,
    model: str,
    route: str,
    purpose: str,
    prompt_chars: int,
    completion_chars: int,
    latency_ms: float,
    status: str = "ok",
    error_type: str | None = None,
    tenant_id: str | None = None,
) -> dict | None:
    """Sync, sessionless — callable from inside the backend wrapper.

    Never raises: a metrics failure must not turn a working completion into a
    failed request.

    Returns the metric it queued, or None if it could not build one. F-02 reads
    that return value to price the call: the token counts here are the ones
    actually measured at the chokepoint, and re-deriving them at the call site
    would produce a second, quietly different number for the same completion.
    """
    try:
        prompt_tokens = estimate_tokens_from_chars(prompt_chars)
        completion_tokens = estimate_tokens_from_chars(completion_chars)
        cost, rate_known = estimate_cost_usd(model, prompt_tokens, completion_tokens)
        metric = {
            "provider": provider,
            "model": model,
            "route": route,
            "purpose": purpose,
            "prompt_chars": prompt_chars,
            "completion_chars": completion_chars,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tokens_estimated": True,
            "cost_usd": cost,
            "rate_known": rate_known,
            "latency_ms": round(latency_ms, 2),
            "status": status,
            "error_type": error_type,
            "tenant_id": tenant_id,
            "at": datetime.now(timezone.utc),
        }
        _PENDING.append(metric)
        return metric
    except Exception:                      # pragma: no cover — defensive
        logger.debug("llmops: metric dropped", exc_info=True)
        return None


async def flush_pending(session: AsyncSession) -> int:
    """Drain queued call metrics into `llm_calls`. Caller commits."""
    from velaris_shared.db.models import LLMCallModel

    flushed = 0
    while _PENDING:
        ev = _PENDING.popleft()
        session.add(LLMCallModel(
            provider=ev["provider"],
            model=ev["model"],
            route=ev["route"],
            purpose=ev["purpose"],
            prompt_tokens=ev["prompt_tokens"],
            completion_tokens=ev["completion_tokens"],
            tokens_estimated=ev["tokens_estimated"],
            cost_usd=ev["cost_usd"],
            latency_ms=ev["latency_ms"],
            status=ev["status"],
            error_type=ev["error_type"],
            tenant_id=ev["tenant_id"],
            created_at=ev["at"],
        ))
        flushed += 1
    return flushed


async def metrics(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    group_by: str = "model",
    tenants: list[str] | None = None,
    include_untenanted: bool = True,
) -> dict[str, Any]:
    """Aggregate recorded calls.

    `tenants` (2026-11-04): only these tenant spellings — a tenant's admin
    sees its own calls. `include_untenanted`: also the calls with no tenant
    (jobs, and every call before 2026-11-04), which are the default tenant's.
    None = every call (the platform operator).

    `group_by` is one of `model`, `provider`, `purpose`, `route` — the four
    axes a cost question is actually asked along ("which model", "which
    provider", "which feature", "local or external").
    """
    from velaris_shared.db.models import LLMCallModel

    column = {
        "model": LLMCallModel.model,
        "provider": LLMCallModel.provider,
        "purpose": LLMCallModel.purpose,
        "route": LLMCallModel.route,
    }.get(group_by)
    if column is None:
        raise ValueError(
            f"group_by must be one of model, provider, purpose, route (got {group_by!r})"
        )

    q = select(
        column.label("key"),
        func.count().label("calls"),
        func.sum(LLMCallModel.prompt_tokens).label("prompt_tokens"),
        func.sum(LLMCallModel.completion_tokens).label("completion_tokens"),
        func.sum(LLMCallModel.cost_usd).label("cost_usd"),
        func.avg(LLMCallModel.latency_ms).label("avg_latency_ms"),
    ).group_by(column)
    if since is not None:
        q = q.where(LLMCallModel.created_at >= since)
    scope = None
    if tenants is not None:
        from sqlalchemy import or_
        scope = LLMCallModel.tenant_id.in_(tenants)
        if include_untenanted:
            scope = or_(scope, LLMCallModel.tenant_id.is_(None))
        q = q.where(scope)

    rows = (await session.execute(q)).all()

    errors_q = select(func.count()).where(LLMCallModel.status != "ok")
    if since is not None:
        errors_q = errors_q.where(LLMCallModel.created_at >= since)
    if scope is not None:
        errors_q = errors_q.where(scope)
    errors = (await session.execute(errors_q)).scalar_one()

    groups = [{
        "key": r.key,
        "calls": r.calls,
        "prompt_tokens": int(r.prompt_tokens or 0),
        "completion_tokens": int(r.completion_tokens or 0),
        "cost_usd": round(float(r.cost_usd or 0.0), 6),
        "avg_latency_ms": round(float(r.avg_latency_ms or 0.0), 2),
    } for r in rows]
    groups.sort(key=lambda g: g["calls"], reverse=True)

    total_calls = sum(g["calls"] for g in groups)
    return {
        "group_by": group_by,
        "since": since.isoformat() if since else None,
        "total_calls": total_calls,
        "total_cost_usd": round(sum(g["cost_usd"] for g in groups), 6),
        "total_tokens": sum(g["prompt_tokens"] + g["completion_tokens"] for g in groups),
        "error_calls": errors,
        "error_rate": round(errors / total_calls, 4) if total_calls else 0.0,
        "tokens_estimated": True,
        "note": (
            "Token counts are estimated from character length — no backend here "
            "reports provider usage. Costs are an order-of-magnitude guide."
        ),
        "groups": groups,
        "scope": "platform" if tenants is None else "tenant",
    }
