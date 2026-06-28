"""HxNexus Auto-Documentation — P42.

Generates two living documents from HxGraph + LLM:

  business_guide  — plain English: what this platform does, who uses it,
                    what cases exist, what stages they go through.
  dev_guide       — technical: API reference, data model, how to extend.

Both are cached in generated_docs and regenerated when the graph changes.
Works without LLM (template-based fallback) — richer with LLM available.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import GraphNodeModel, GraphEdgeModel, GeneratedDocModel

logger = logging.getLogger(__name__)

_BUSINESS_SYSTEM = (
    "You are a business analyst writing documentation for non-technical stakeholders. "
    "Given a description of a BPM platform's case types, stages, and steps, "
    "write a clear, friendly business guide. "
    "Use plain English. No jargon. No code. "
    "Structure: intro paragraph, then one section per case type."
)

_DEV_SYSTEM = (
    "You are a senior engineer writing technical documentation for developers. "
    "Given a description of a BPM platform's modules, API endpoints, and data model, "
    "write a concise developer guide. "
    "Structure: platform overview, API surface, data model, how to extend."
)


async def _current_node_count(session: AsyncSession) -> int:
    rows = (await session.execute(select(GraphNodeModel))).all()
    return len(rows)


async def _is_stale(session: AsyncSession, doc_type: str) -> bool:
    """Returns True if the cached doc needs regenerating."""
    doc = (await session.execute(
        select(GeneratedDocModel).where(GeneratedDocModel.doc_type == doc_type)
    )).scalar_one_or_none()
    if not doc:
        return True
    current = await _current_node_count(session)
    return current != doc.node_count


async def _save_doc(session: AsyncSession, doc_type: str, content: str) -> None:
    node_count = await _current_node_count(session)
    existing = (await session.execute(
        select(GeneratedDocModel).where(GeneratedDocModel.doc_type == doc_type)
    )).scalar_one_or_none()

    if existing:
        existing.content = content
        existing.generated_at = datetime.now(timezone.utc)
        existing.node_count = node_count
    else:
        session.add(GeneratedDocModel(
            doc_type=doc_type,
            content=content,
            node_count=node_count,
        ))
    await session.commit()


# ── Context builders ──────────────────────────────────────────────────────────

async def _build_business_context(session: AsyncSession) -> str:
    """Build a plain-English description of the platform for the LLM."""
    case_types = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.node_type == "case_type")
    )).scalars().all()

    lines = ["This platform manages the following business processes:\n"]

    for ct in case_types:
        props = ct.properties or {}
        lines.append(f"## {ct.label}")
        if ct.summary:
            lines.append(ct.summary)

        # Get stages for this case type
        outgoing = (await session.execute(
            select(GraphEdgeModel).where(
                GraphEdgeModel.from_node_id == ct.id,
                GraphEdgeModel.edge_type == "has_stage",
            )
        )).scalars().all()

        stage_lines = []
        for edge in outgoing:
            stage = await session.get(GraphNodeModel, edge.to_node_id)
            if stage:
                sp = stage.properties or {}
                step_count = sp.get("step_count", 0)
                stage_lines.append(f"  - {stage.label} ({step_count} steps)")

        if stage_lines:
            lines.append("Stages: " + ", ".join(
                (await session.get(GraphNodeModel, e.to_node_id)).label  # type: ignore
                for e in outgoing
                if await session.get(GraphNodeModel, e.to_node_id)
            ))
        lines.append("")

    return "\n".join(lines)


async def _build_dev_context(session: AsyncSession) -> str:
    """Build a technical description of the platform for the LLM."""
    modules = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.node_type == "module")
    )).scalars().all()

    endpoints = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.node_type == "endpoint")
    )).scalars().all()

    forms = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.node_type == "form")
    )).scalars().all()

    lines = [
        f"Platform: Velaris BPM",
        f"Studio modules ({len(modules)}): " + ", ".join(m.label for m in modules[:20]),
        f"API endpoints ({len(endpoints)}): " + ", ".join(e.label for e in endpoints[:20]),
        f"Form definitions ({len(forms)}): " + ", ".join(f.label for f in forms),
        "",
        "Architecture: FastAPI (Python) backend + React/TypeScript frontend.",
        "DB: PostgreSQL with SQLAlchemy ORM. Auth: JWT. Storage: local or MinIO.",
        "AI: HxNexus (Ollama/OpenAI/Anthropic backends). Knowledge: HxGraph.",
        "Deployment: Docker Compose (development), Helm (production).",
    ]
    return "\n".join(lines)


# ── Template fallbacks (no LLM) ───────────────────────────────────────────────

async def _business_guide_template(session: AsyncSession) -> str:
    case_types = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.node_type == "case_type")
    )).scalars().all()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# Velaris Platform — Business Guide",
        f"*Generated {now}*",
        "",
        "Velaris is an enterprise BPM (Business Process Management) platform. "
        "It manages structured business processes called **cases** — each case "
        "moves through a defined set of **stages**, with work items (**steps**) "
        "assigned to operators at each stage.",
        "",
        "---",
        "",
        "## Case Types",
        "",
    ]

    for ct in case_types:
        props = ct.properties or {}
        lines.append(f"### {ct.label}")
        if ct.summary:
            lines.append(f"*{ct.summary}*")
            lines.append("")

        outgoing = (await session.execute(
            select(GraphEdgeModel).where(
                GraphEdgeModel.from_node_id == ct.id,
                GraphEdgeModel.edge_type == "has_stage",
            )
        )).scalars().all()

        if outgoing:
            lines.append("**Stages:**")
            for edge in outgoing:
                stage = await session.get(GraphNodeModel, edge.to_node_id)
                if stage:
                    sp = stage.properties or {}
                    sla = f" — SLA: {sp['sla_hours']}h" if sp.get("sla_hours") else ""
                    lines.append(f"- {stage.label}{sla}")
        lines.append("")

    lines += [
        "---",
        "*This guide is auto-generated from the live platform. "
        "It updates automatically as case types, stages, and forms change.*",
    ]
    return "\n".join(lines)


async def _dev_guide_template(session: AsyncSession) -> str:
    modules = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.node_type == "module")
    )).scalars().all()
    endpoints = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.node_type == "endpoint")
    )).scalars().all()
    case_types = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.node_type == "case_type")
    )).scalars().all()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# Velaris Platform — Developer Guide",
        f"*Generated {now}*",
        "",
        "## Platform Overview",
        "",
        "Velaris is an open-source BPM platform built on FastAPI + React.",
        "- **Backend:** `services/case-service` (Python, FastAPI, SQLAlchemy async)",
        "- **Frontend:** `studio/` (React 18, TypeScript, Vite)",
        "- **DB:** PostgreSQL (production), SQLite (tests)",
        "- **AI:** HxNexus — pluggable LLM backend (Ollama / OpenAI / Anthropic)",
        "- **Graph:** HxGraph — live semantic knowledge graph (`/api/v1/graph`)",
        "",
        "## Studio Modules",
        "",
    ]

    by_category: dict[str, list] = {}
    for m in modules:
        cat = (m.properties or {}).get("category", "Other")
        by_category.setdefault(cat, []).append(m)

    for cat, mods in sorted(by_category.items()):
        lines.append(f"**{cat}:** " + ", ".join(f"`{m.label}` ({m.name})" for m in mods))

    lines += ["", "## Key API Endpoints", ""]
    for ep in endpoints[:30]:
        lines.append(f"- `{ep.label}`")

    lines += [
        "",
        "## Data Model",
        "",
        f"- **case_types** — {len(case_types)} case type(s) defined",
        "- **case_instances** — running cases (status, priority, stage, data JSONB)",
        "- **graph_nodes / graph_edges** — HxGraph knowledge graph",
        "- **trace_events** — HxStream live event log",
        "- **bpm_concepts** — BPM tool → Velaris translation knowledge base",
        "",
        "## Adding a New Phase",
        "",
        "1. Write migration `NNN_phaseNN_name.sql` → apply with `psql`",
        "2. Add ORM model to `case_service/db/models.py`",
        "3. Create router `case_service/api/routers/name.py`",
        "4. Register in `main.py` + add entry to `sitemap.py`",
        "5. Add Vite proxy in `studio/vite.config.ts`",
        "6. Create Studio module `studio/src/modules/name/`",
        "7. Wire route in `main.tsx` + sidebar in `AppLayout.tsx`",
        "8. Write 20+ tests in `tests/phaseNN/`",
        "",
        "---",
        "*Auto-generated from live platform state. Run `POST /api/v1/hxnexus/docs/regenerate` to refresh.*",
    ]
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

async def get_business_guide(session: AsyncSession, force: bool = False, llm=None) -> str:
    if not force and not await _is_stale(session, "business_guide"):
        doc = (await session.execute(
            select(GeneratedDocModel).where(GeneratedDocModel.doc_type == "business_guide")
        )).scalar_one_or_none()
        if doc:
            return doc.content

    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            pass

    if llm and getattr(llm, "available", False):
        try:
            context = await _build_business_context(session)
            content = await llm.complete(
                f"Write a business guide for this platform:\n\n{context}",
                system=_BUSINESS_SYSTEM,
                temperature=0.3,
            )
            if content:
                await _save_doc(session, "business_guide", content)
                return content
        except Exception as e:
            logger.warning("autodoc.business_guide: LLM failed, using template: %s", e)

    # Template fallback
    content = await _business_guide_template(session)
    await _save_doc(session, "business_guide", content)
    return content


async def get_dev_guide(session: AsyncSession, force: bool = False, llm=None) -> str:
    if not force and not await _is_stale(session, "dev_guide"):
        doc = (await session.execute(
            select(GeneratedDocModel).where(GeneratedDocModel.doc_type == "dev_guide")
        )).scalar_one_or_none()
        if doc:
            return doc.content

    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            pass

    if llm and getattr(llm, "available", False):
        try:
            context = await _build_dev_context(session)
            content = await llm.complete(
                f"Write a developer guide for this platform:\n\n{context}",
                system=_DEV_SYSTEM,
                temperature=0.2,
            )
            if content:
                await _save_doc(session, "dev_guide", content)
                return content
        except Exception as e:
            logger.warning("autodoc.dev_guide: LLM failed, using template: %s", e)

    content = await _dev_guide_template(session)
    await _save_doc(session, "dev_guide", content)
    return content
