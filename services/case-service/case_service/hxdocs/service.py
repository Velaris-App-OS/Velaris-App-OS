"""P58 HxDocs — Living Documentation service."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from case_service.db.models import (
    HxDocsSpaceModel,
    HxDocsArticleModel,
    FormDefinitionModel,
    HxDocsArticleVersionModel,
    CaseTypeModel,
    CaseInstanceModel,
    GraphNodeModel,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:80]


def _word_count(blocks: list) -> int:
    total = 0
    for b in blocks:
        text = b.get("text", "")
        if text:
            total += len(text.split())
    return total


# ── Spaces ────────────────────────────────────────────────────────────────────

async def list_spaces(session: AsyncSession, tenant_id: str) -> list[HxDocsSpaceModel]:
    rows = (await session.execute(
        select(HxDocsSpaceModel)
        .where(HxDocsSpaceModel.tenant_id == tenant_id)
        .order_by(HxDocsSpaceModel.name)
    )).scalars().all()
    return list(rows)


async def create_space(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    description: str,
    is_public: bool,
    created_by: str,
) -> HxDocsSpaceModel:
    slug = _slugify(name)
    space = HxDocsSpaceModel(
        tenant_id=tenant_id, name=name, slug=slug,
        description=description, is_public=is_public, created_by=created_by,
    )
    session.add(space)
    await session.flush()
    return space


async def get_space(session: AsyncSession, space_id: uuid.UUID, tenant_id: str) -> HxDocsSpaceModel | None:
    result = await session.execute(
        select(HxDocsSpaceModel).where(
            HxDocsSpaceModel.id == space_id,
            HxDocsSpaceModel.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


# ── Articles ──────────────────────────────────────────────────────────────────

async def list_articles(
    session: AsyncSession,
    space_id: uuid.UUID,
    tenant_id: str,
    status: str | None = None,
) -> list[HxDocsArticleModel]:
    q = (
        select(HxDocsArticleModel)
        .where(
            HxDocsArticleModel.space_id == space_id,
            HxDocsArticleModel.tenant_id == tenant_id,
        )
        .order_by(HxDocsArticleModel.updated_at.desc())
    )
    if status:
        q = q.where(HxDocsArticleModel.status == status)
    return list((await session.execute(q)).scalars().all())


async def search_articles(
    session: AsyncSession,
    tenant_id: str,
    q: str,
    limit: int = 20,
) -> list[HxDocsArticleModel]:
    rows = (await session.execute(
        select(HxDocsArticleModel)
        .where(
            HxDocsArticleModel.tenant_id == tenant_id,
            or_(
                HxDocsArticleModel.title.ilike(f"%{q}%"),
                HxDocsArticleModel.source_concept.ilike(f"%{q}%"),
            ),
        )
        .order_by(HxDocsArticleModel.updated_at.desc())
        .limit(limit)
    )).scalars().all()
    return list(rows)


async def create_article(
    session: AsyncSession,
    space: HxDocsSpaceModel,
    title: str,
    content: list,
    tags: list,
    created_by: str,
    auto_generated: bool = False,
    source_concept: str | None = None,
) -> HxDocsArticleModel:
    slug = _slugify(title)
    article = HxDocsArticleModel(
        space_id=space.id, tenant_id=space.tenant_id,
        title=title, slug=slug, content=content,
        word_count=_word_count(content),
        tags=tags, created_by=created_by, updated_by=created_by,
        auto_generated=auto_generated, source_concept=source_concept,
    )
    session.add(article)
    await session.flush()
    return article


async def get_article(
    session: AsyncSession,
    article_id: uuid.UUID,
    tenant_id: str,
) -> HxDocsArticleModel | None:
    result = await session.execute(
        select(HxDocsArticleModel).where(
            HxDocsArticleModel.id == article_id,
            HxDocsArticleModel.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def update_article(
    session: AsyncSession,
    article: HxDocsArticleModel,
    title: str | None,
    content: list | None,
    tags: list | None,
    updated_by: str,
    save_version: bool = False,
) -> HxDocsArticleModel:
    if save_version:
        snap = HxDocsArticleVersionModel(
            article_id=article.id, tenant_id=article.tenant_id,
            version=article.version, title=article.title,
            content=article.content, package_version=article.package_version,
            saved_by=updated_by,
        )
        session.add(snap)
        article.version += 1

    if title is not None:
        article.title = title
    if content is not None:
        article.content = content
        article.word_count = _word_count(content)
    if tags is not None:
        article.tags = tags
    article.updated_by = updated_by
    article.updated_at = _utcnow()
    await session.flush()
    return article


async def publish_article(
    session: AsyncSession,
    article: HxDocsArticleModel,
    is_public: bool,
    actor: str,
) -> HxDocsArticleModel:
    article.status = "published"
    article.is_public = is_public
    article.updated_by = actor
    article.updated_at = _utcnow()
    await session.flush()
    return article


async def delete_article(session: AsyncSession, article: HxDocsArticleModel) -> None:
    await session.delete(article)
    await session.flush()


async def get_versions(
    session: AsyncSession,
    article_id: uuid.UUID,
) -> list[HxDocsArticleVersionModel]:
    rows = (await session.execute(
        select(HxDocsArticleVersionModel)
        .where(HxDocsArticleVersionModel.article_id == article_id)
        .order_by(HxDocsArticleVersionModel.version.desc())
    )).scalars().all()
    return list(rows)


# ── AI generation ─────────────────────────────────────────────────────────────

async def generate_article_content(
    session: AsyncSession,
    concept: str,
    tenant_id: str,
) -> list:
    """Generate block-based article content from a concept using HxNexus + HxGraph."""
    # Gather context from the graph
    graph_context = ""
    try:
        rows = (await session.execute(
            select(GraphNodeModel)
            .where(
                or_(
                    GraphNodeModel.name.ilike(f"%{concept}%"),
                    GraphNodeModel.label.ilike(f"%{concept}%"),
                ),
                or_(GraphNodeModel.tenant_id == tenant_id, GraphNodeModel.tenant_id.is_(None)),
            )
            .limit(5)
        )).scalars().all()
        if rows:
            graph_context = "\n".join(
                f"- {n.node_type}: {n.label} — {n.summary or 'no summary'}"
                for n in rows
            )
    except Exception:
        pass

    # Try LLM generation
    try:
        from case_service.hxnexus.factory import get_llm_backend
        llm = get_llm_backend()
        prompt = f"""You are a technical documentation writer for a BPM platform called Helix.
Write a comprehensive documentation article about: {concept}

Platform context from HxGraph:
{graph_context or 'No graph data available.'}

Generate a structured article with these sections:
1. Overview (2-3 sentences)
2. Key Concepts (bullet points)
3. How It Works (step-by-step)
4. Common Use Cases (2-3 examples)

Format EACH section as a JSON block. Return a JSON array of blocks:
[
  {{"id":"b1","type":"heading","level":1,"text":"<title>"}},
  {{"id":"b2","type":"paragraph","text":"<overview text>"}},
  {{"id":"b3","type":"heading","level":2,"text":"Key Concepts"}},
  {{"id":"b4","type":"paragraph","text":"<bullet points as plain text>"}},
  {{"id":"b5","type":"heading","level":2,"text":"How It Works"}},
  {{"id":"b6","type":"paragraph","text":"<step by step>"}},
  {{"id":"b7","type":"heading","level":2,"text":"Common Use Cases"}},
  {{"id":"b8","type":"paragraph","text":"<use cases>"}}
]

Return ONLY the JSON array, no other text."""

        import json
        raw = await llm.complete(prompt)
        raw = raw.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rsplit("```", 1)[0].strip()
        blocks = json.loads(raw)
        if isinstance(blocks, list):
            return blocks
    except Exception as e:
        logger.warning("HxDocs LLM generation failed: %s", e)

    # Fallback: deterministic blocks from graph context
    blocks = [
        {"id": "b1", "type": "heading", "level": 1, "text": concept},
        {"id": "b2", "type": "callout", "text": f"This article was auto-generated from HxGraph data for: {concept}"},
    ]
    if graph_context:
        blocks.append({"id": "b3", "type": "heading", "level": 2, "text": "Platform Context"})
        blocks.append({"id": "b4", "type": "paragraph", "text": graph_context})
    blocks.append({"id": "b5", "type": "paragraph", "text": f"Edit this article to add more detail about {concept}."})
    return blocks


# ── Live data resolution ──────────────────────────────────────────────────────

async def resolve_live_blocks(
    session: AsyncSession,
    blocks: list,
    tenant_id: str,
) -> list:
    """Resolve live_data blocks by fetching real values from the DB."""
    result = []
    for block in blocks:
        if block.get("type") != "live_data":
            result.append(block)
            continue
        embed = block.get("embed_type", "")
        resolved = dict(block)
        try:
            if embed == "case_count":
                ct_id = block.get("case_type_id")
                q = select(func.count()).select_from(CaseInstanceModel)
                if ct_id:
                    q = q.where(CaseInstanceModel.case_type_id == uuid.UUID(ct_id))
                count = (await session.execute(q)).scalar() or 0
                resolved["_live"] = {"count": count, "label": block.get("label", "Cases")}
            elif embed == "case_status_breakdown":
                ct_id = block.get("case_type_id")
                q = (
                    select(CaseInstanceModel.status, func.count().label("cnt"))
                    .group_by(CaseInstanceModel.status)
                )
                if ct_id:
                    q = q.where(CaseInstanceModel.case_type_id == uuid.UUID(ct_id))
                rows = (await session.execute(q)).all()
                resolved["_live"] = {"breakdown": {r.status: r.cnt for r in rows}}
            elif embed == "graph_node":
                concept = block.get("concept", "")
                node = (await session.execute(
                    select(GraphNodeModel)
                    .where(
                        or_(
                            GraphNodeModel.name.ilike(f"%{concept}%"),
                            GraphNodeModel.label.ilike(f"%{concept}%"),
                        )
                    )
                    .limit(1)
                )).scalar_one_or_none()
                if node:
                    resolved["_live"] = {
                        "name": node.name, "label": node.label,
                        "node_type": node.node_type, "summary": node.summary,
                    }
        except Exception as e:
            logger.debug("live block resolve error: %s", e)
            resolved["_live"] = {"error": str(e)}
        result.append(resolved)
    return result


# ── Case Lifecycle Narrative Generator ────────────────────────────────────────

_STEP_TYPE_LABELS = {
    "user_task":    "User Task",
    "approval":     "Approval",
    "automated":    "Automated Step",
    "notification": "Notification",
    "decision":     "Decision Gateway",
    "subprocess":   "Sub-process",
    "service_task": "Service Call",
}

_ASSIGNMENT_LABELS = {
    "queue_based":   "assigned from a work queue",
    "round_robin":   "assigned via round-robin",
    "manual":        "manually assigned",
    "self_assign":   "self-assigned by any eligible user",
    "specific_user": "assigned to a specific user",
}


def _field_narrative(field: dict) -> str:
    ftype = field.get("type", "text")
    label = field.get("label", field.get("field_key", "field"))
    required = "required" if field.get("required") else "optional"
    desc = field.get("description") or field.get("placeholder") or ""
    opts = field.get("options", [])
    line = f"**{label}** ({ftype}, {required})"
    if desc:
        line += f" — {desc}"
    if opts:
        opt_labels = [o.get("label", o.get("value", "")) for o in opts[:6]]
        line += f" *(options: {', '.join(opt_labels)}{'…' if len(opts) > 6 else ''})*"
    return line


async def _load_form(session: AsyncSession, form_id: str) -> dict | None:
    try:
        result = await session.execute(
            select(FormDefinitionModel).where(
                FormDefinitionModel.id == uuid.UUID(form_id)
            )
        )
        return result.scalar_one_or_none()
    except Exception:
        return None


def _build_lifecycle_blocks_deterministic(ct_name: str, definition: dict, forms: dict[str, dict]) -> list[dict]:
    """Build narrative article blocks from a case type definition without LLM."""
    import time
    blocks: list[dict] = []
    bid = lambda: f"b{int(time.time()*1000)}-{len(blocks)}"

    stages = definition.get("stages", [])
    total_stages = len(stages)
    total_steps  = sum(len(s.get("steps", [])) for s in stages)

    # Header
    blocks.append({"id": bid(), "type": "heading", "level": 1,
                   "text": f"{ct_name} — Full Lifecycle Guide"})
    blocks.append({"id": bid(), "type": "callout",
                   "text": f"📡 This article was generated directly from the live case type definition. It describes every stage and step in the {ct_name} lifecycle exactly as configured."})
    blocks.append({"id": bid(), "type": "paragraph",
                   "text": f"The {ct_name} process flows through {total_stages} stage{'s' if total_stages != 1 else ''} and {total_steps} step{'s' if total_steps != 1 else ''}. Each stage represents a distinct phase of the case, and each step within a stage is a specific action that must be completed before the case advances."})

    for stage_idx, stage in enumerate(sorted(stages, key=lambda s: s.get("order", 999)), 1):
        stage_name  = stage.get("name", stage.get("id", f"Stage {stage_idx}"))
        stage_type  = stage.get("stage_type", "linear")
        steps       = stage.get("steps", [])

        blocks.append({"id": bid(), "type": "heading", "level": 2,
                       "text": f"Stage {stage_idx}: {stage_name}"})

        # Stage intro sentence
        stage_intro = f"The **{stage_name}** stage"
        if stage_type == "parallel":
            stage_intro += " runs multiple steps in parallel — all must complete before the case moves forward."
        elif stage_type == "optional":
            stage_intro += " is optional and may be skipped depending on case conditions."
        else:
            stage_intro += f" runs {len(steps)} step{'s' if len(steps) != 1 else ''} in sequence."
        blocks.append({"id": bid(), "type": "paragraph", "text": stage_intro})

        for step_idx, step in enumerate(steps, 1):
            step_name = step.get("name", step.get("id", f"Step {step_idx}"))
            step_type = step.get("step_type", "user_task")
            assignment = step.get("assignment", {})
            form_id    = step.get("form_id") or step.get("formId")
            description = step.get("description", "")

            blocks.append({"id": bid(), "type": "heading", "level": 3,
                           "text": f"Step {step_idx}: {step_name}"})

            # Step type + assignment line
            type_label = _STEP_TYPE_LABELS.get(step_type, step_type.replace("_", " ").title())
            assign_str = _ASSIGNMENT_LABELS.get(assignment.get("strategy", ""), "")
            meta_parts = [f"**Type:** {type_label}"]
            if assign_str:
                meta_parts.append(f"**Assigned:** {assign_str}")
            if step.get("required") is False:
                meta_parts.append("**Optional**")
            blocks.append({"id": bid(), "type": "paragraph",
                           "text": "  ·  ".join(meta_parts)})

            # Step description
            if description:
                blocks.append({"id": bid(), "type": "paragraph", "text": description})

            # Narrative sentence for step type
            if step_type == "user_task":
                actor = "A staff member" if not assign_str else f"A staff member ({assign_str})"
                blocks.append({"id": bid(), "type": "paragraph",
                               "text": f"{actor} opens this step in their Work Center, completes the required actions, and marks it complete to advance the case."})
            elif step_type == "approval":
                blocks.append({"id": bid(), "type": "paragraph",
                               "text": "An approver reviews the case data and either **approves** (advancing to the next step) or **rejects** (returning the case for rework or closure)."})
            elif step_type == "automated":
                blocks.append({"id": bid(), "type": "paragraph",
                               "text": "This step runs automatically — no human action is required. The system executes the configured logic and advances the case when complete."})
            elif step_type == "notification":
                blocks.append({"id": bid(), "type": "paragraph",
                               "text": "The system sends a notification (email, SMS, or in-app) to the relevant parties. The case advances immediately after the notification is dispatched."})

            # Form fields
            if form_id and form_id in forms:
                form_def = forms[form_id]
                form_name = getattr(form_def, "name", "Form") if hasattr(form_def, "name") else form_id
                def_json  = form_def.definition_json if hasattr(form_def, "definition_json") else {}
                sections  = def_json.get("sections", [])

                all_fields = [f for sec in sections for f in sec.get("fields", [])]
                required_fields = [f for f in all_fields if f.get("required")]
                optional_fields = [f for f in all_fields if not f.get("required")]

                blocks.append({"id": bid(), "type": "paragraph",
                               "text": f"**Form: {form_name}** ({len(all_fields)} field{'s' if len(all_fields) != 1 else ''})"})

                if required_fields:
                    field_lines = "\n".join(f"• {_field_narrative(f)}" for f in required_fields)
                    blocks.append({"id": bid(), "type": "paragraph",
                                   "text": f"The following fields **must** be completed:\n{field_lines}"})

                if optional_fields:
                    field_lines = "\n".join(f"• {_field_narrative(f)}" for f in optional_fields)
                    blocks.append({"id": bid(), "type": "paragraph",
                                   "text": f"These fields are optional:\n{field_lines}"})

                if sections and len(sections) > 1:
                    section_names = [s.get("title", f"Section {i+1}") for i, s in enumerate(sections)]
                    blocks.append({"id": bid(), "type": "paragraph",
                                   "text": f"The form is organised into {len(sections)} sections: {', '.join(section_names)}."})

        # Stage transition note
        if stage_idx < total_stages:
            next_stage = stages[stage_idx] if stage_idx < len(stages) else None
            next_name  = next_stage.get("name", "the next stage") if next_stage else "the next stage"
            blocks.append({"id": bid(), "type": "callout",
                           "text": f"✅ Once all steps in {stage_name} are complete, the case automatically advances to **{next_name}**."})

    # Closing
    blocks.append({"id": bid(), "type": "heading", "level": 2, "text": "Summary"})
    stage_list = ", ".join(s.get("name", s.get("id")) for s in stages)
    blocks.append({"id": bid(), "type": "paragraph",
                   "text": f"The {ct_name} lifecycle progresses through: {stage_list}. Each stage has clearly defined steps, responsibilities, and forms to ensure consistency and auditability across every case."})
    blocks.append({"id": bid(), "type": "live_data", "embed_type": "case_count",
                   "label": f"Active {ct_name} cases"})

    return blocks


async def generate_lifecycle_article(
    session: AsyncSession,
    case_type_id: uuid.UUID,
    tenant_id: str,
) -> tuple[list | None, str]:
    """Build a full narrative lifecycle article for a case type. Returns (blocks, title)."""
    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        return None, ""

    definition = ct.definition_json or {}
    stages = definition.get("stages", [])

    # Collect all unique form_ids referenced by steps
    form_ids: set[str] = set()
    for stage in stages:
        for step in stage.get("steps", []):
            fid = step.get("form_id") or step.get("formId")
            if fid:
                form_ids.add(str(fid))

    # Load all referenced forms in one query
    forms: dict[str, Any] = {}
    for fid in form_ids:
        form = await _load_form(session, fid)
        if form:
            forms[fid] = form

    blocks = _build_lifecycle_blocks_deterministic(ct.name, definition, forms)
    title  = f"{ct.name} — Lifecycle Guide"
    return blocks, title


async def find_lifecycle_articles_for_case_type(
    session: AsyncSession,
    case_type_id: uuid.UUID,
) -> list[HxDocsArticleModel]:
    """Find lifecycle articles by UUID source_concept OR by name-based title match."""
    # Load the case type name so we can also match old articles that stored the name
    ct = await session.get(CaseTypeModel, case_type_id)
    ct_name = ct.name if ct else ""

    conditions = [
        HxDocsArticleModel.source_concept == str(case_type_id),  # new: UUID stored
    ]
    if ct_name:
        # Old articles store the case type name (possibly trimmed) as source_concept
        conditions.append(HxDocsArticleModel.source_concept.ilike(f"%{ct_name}%"))

    rows = (await session.execute(
        select(HxDocsArticleModel).where(
            HxDocsArticleModel.auto_generated == True,  # noqa: E712
            or_(*conditions),
        )
    )).scalars().all()
    return list(rows)


async def regenerate_lifecycle_article(
    session: AsyncSession,
    article: HxDocsArticleModel,
    actor: str = "system",
) -> HxDocsArticleModel:
    """Rebuild a lifecycle article from the current case type definition, snapshot the old version first."""
    ct_id: uuid.UUID | None = None
    # source_concept is either a UUID string (new) or a title like "X — Lifecycle Guide" (old)
    try:
        ct_id = uuid.UUID(article.source_concept or "")
    except ValueError:
        # Fallback: match by case type name from the title "Name — Lifecycle Guide"
        name = (article.source_concept or "").replace(" — Lifecycle Guide", "").strip()
        if name:
            row = (await session.execute(
                select(CaseTypeModel).where(CaseTypeModel.name.ilike(f"%{name}%")).limit(1)
            )).scalar_one_or_none()
            if row:
                ct_id = row.id
    if ct_id is None:
        return article  # cannot resolve case type — leave unchanged

    new_blocks, new_title = await generate_lifecycle_article(session, ct_id, article.tenant_id)
    if new_blocks is None:
        return article  # case type deleted — leave article unchanged

    # Snapshot current version before overwriting
    snap = HxDocsArticleVersionModel(
        article_id=article.id, tenant_id=article.tenant_id,
        version=article.version, title=article.title,
        content=article.content, package_version=article.package_version,
        saved_by=actor,
    )
    session.add(snap)
    article.version      += 1
    article.title        = new_title
    article.content      = new_blocks
    article.word_count   = _word_count(new_blocks)
    article.updated_by   = actor
    article.updated_at   = _utcnow()
    # Migrate source_concept to UUID so future find queries are fast and exact
    article.source_concept = str(ct_id)
    await session.flush()
    return article
