"""HxAnalytics — Query engine: structured queries + HxNexus NL interpretation.

Structured query format:
  {
    "metric":    "count" | "avg_resolution" | "sla_breach_rate" | "throughput",
    "group_by":  "status" | "priority" | "case_type" | "day" | "week" | "month",
    "filters":   { "status": "open", "priority": "high", "days": 30 },
    "limit":     20
  }

Natural language queries go through HxNexus, which maps them to a structured query.
Falls back to keyword matching when LLM is unavailable.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import CaseInstanceModel, CaseTypeModel, CaseSLAInstanceModel
from case_service.analytics.metrics import platform_snapshot, cases_over_time, sla_performance

logger = logging.getLogger(__name__)

_NL_SYSTEM = """You are HxNexus, analysing a business analytics question about a case management platform.

Map the question to a structured analytics query and respond with ONLY valid JSON:
{
  "metric":   "count" | "avg_resolution" | "sla_breach_rate" | "throughput",
  "group_by": "status" | "priority" | "case_type" | "day" | "week" | "month" | null,
  "filters":  { "days": <int>, "status": "<str>", "priority": "<str>" },
  "chart_type": "bar" | "line" | "pie" | "number" | "table",
  "title": "<short descriptive title for the chart>"
}
If you cannot map it, return { "metric": "snapshot", "chart_type": "number", "title": "Platform Overview" }"""


async def nl_query(question: str, session: AsyncSession) -> dict:
    """Interpret a plain-English question and return chart-ready data."""
    structured = await _parse_nl(question)
    data = await run_structured(structured, session)
    data["question"] = question
    data["interpreted_as"] = structured
    return data


async def run_structured(query_def: dict, session: AsyncSession) -> dict:
    """Execute a structured query and return chart-ready result."""
    metric   = query_def.get("metric", "count")
    group_by = query_def.get("group_by")
    filters  = query_def.get("filters", {})
    limit    = query_def.get("limit", 20)
    chart_type = query_def.get("chart_type", "bar")
    title    = query_def.get("title", metric.replace("_", " ").title())
    days     = int(filters.get("days", 30))
    since    = datetime.now(timezone.utc) - timedelta(days=days)

    # ── Snapshot ──────────────────────────────────────────────────────────────
    if metric == "snapshot":
        snap = await platform_snapshot(session)
        return {"title": title, "chart_type": "number", "data": snap, "series": []}

    # ── SLA breach rate ───────────────────────────────────────────────────────
    if metric == "sla_breach_rate":
        perf = await sla_performance(session, days=days)
        return {
            "title":      title or f"SLA Breach Rate (last {days} days)",
            "chart_type": chart_type or "line",
            "series": [
                {"label": r["date"], "value": r["breach_pct"]}
                for r in perf["series"]
            ],
            "data": perf,
        }

    # ── Throughput (cases over time) ──────────────────────────────────────────
    if metric == "throughput":
        series = await cases_over_time(session, days=days)
        return {
            "title":      title or f"Case Throughput (last {days} days)",
            "chart_type": chart_type or "line",
            "series":     [{"label": r["date"], "value": r["count"]} for r in series],
            "data":       {"series": series},
        }

    # ── Count / avg_resolution grouped ───────────────────────────────────────
    stmt = select(CaseInstanceModel)
    if filters.get("status"):
        stmt = stmt.where(CaseInstanceModel.status == filters["status"])
    if filters.get("priority"):
        stmt = stmt.where(CaseInstanceModel.priority == filters["priority"])
    stmt = stmt.where(CaseInstanceModel.created_at >= since)

    if metric == "avg_resolution":
        rows = (await session.execute(
            stmt.where(CaseInstanceModel.resolved_at.is_not(None))
        )).scalars().all()
        if not rows:
            return {"title": title, "chart_type": "number", "series": [], "data": {"value": None, "unit": "hours"}}
        durations = [
            (r.resolved_at - r.created_at).total_seconds() / 3600
            for r in rows if r.resolved_at
        ]
        avg = round(sum(durations) / len(durations), 1) if durations else 0
        return {"title": title, "chart_type": "number", "series": [], "data": {"value": avg, "unit": "hours", "sample_size": len(durations)}}

    # Default: count grouped
    group_col, label_fn = _resolve_group(group_by)
    if group_col is None:
        total = (await session.execute(select(func.count()).select_from(CaseInstanceModel).where(CaseInstanceModel.created_at >= since))).scalar_one()
        return {"title": title, "chart_type": "number", "series": [], "data": {"value": total}}

    agg_stmt = (
        select(group_col.label("grp"), func.count().label("n"))
        .where(CaseInstanceModel.created_at >= since)
    )
    if filters.get("status"):
        agg_stmt = agg_stmt.where(CaseInstanceModel.status == filters["status"])
    agg_stmt = agg_stmt.group_by(group_col).order_by(func.count().desc()).limit(limit)

    if group_by == "case_type":
        agg_stmt = (
            select(CaseTypeModel.name.label("grp"), func.count(CaseInstanceModel.id).label("n"))
            .join(CaseInstanceModel, CaseInstanceModel.case_type_id == CaseTypeModel.id)
            .where(CaseInstanceModel.created_at >= since)
            .group_by(CaseTypeModel.name)
            .order_by(func.count(CaseInstanceModel.id).desc())
            .limit(limit)
        )

    rows = (await session.execute(agg_stmt)).all()
    series = [{"label": str(r.grp) if r.grp else "unknown", "value": r.n} for r in rows]
    return {"title": title, "chart_type": chart_type or "bar", "series": series, "data": {"rows": len(series)}}


def _resolve_group(group_by: str | None):
    mapping = {
        "status":   CaseInstanceModel.status,
        "priority": CaseInstanceModel.priority,
        "day":      func.date(CaseInstanceModel.created_at),
        "week":     func.strftime("%Y-W%W", CaseInstanceModel.created_at),
        "month":    func.strftime("%Y-%m", CaseInstanceModel.created_at),
    }
    return mapping.get(group_by or ""), None


async def _parse_nl(question: str) -> dict:
    """Use HxNexus to interpret an NL question. Falls back to keyword heuristics."""
    try:
        from case_service.hxnexus.factory import generate_json, check_ai_available
        if await check_ai_available():
            result = await generate_json(question, system=_NL_SYSTEM, temperature=0.1)
            if result and "metric" in result:
                return result
    except Exception as exc:
        logger.debug("NL query LLM failed: %s", exc)

    # Keyword fallback
    q = question.lower()
    if any(w in q for w in ("sla", "breach", "overdue")):
        return {"metric": "sla_breach_rate", "chart_type": "line", "filters": {"days": 30}, "title": "SLA Breach Rate"}
    if any(w in q for w in ("throughput", "created", "new cases", "volume")):
        return {"metric": "throughput", "chart_type": "line", "filters": {"days": 30}, "title": "Case Volume Over Time"}
    if any(w in q for w in ("resolution", "resolve", "time to close", "average")):
        return {"metric": "avg_resolution", "chart_type": "number", "filters": {"days": 30}, "title": "Avg Resolution Time"}
    if any(w in q for w in ("priority", "high", "critical", "urgent")):
        return {"metric": "count", "group_by": "priority", "chart_type": "pie", "filters": {"days": 30}, "title": "Cases by Priority"}
    if any(w in q for w in ("type", "kind", "category")):
        return {"metric": "count", "group_by": "case_type", "chart_type": "bar", "filters": {"days": 30}, "title": "Cases by Type"}
    if any(w in q for w in ("status", "open", "closed", "resolved")):
        return {"metric": "count", "group_by": "status", "chart_type": "bar", "filters": {"days": 30}, "title": "Cases by Status"}

    return {"metric": "snapshot", "chart_type": "number", "filters": {}, "title": "Platform Overview"}
