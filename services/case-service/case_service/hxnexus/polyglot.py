"""HxNexus Polyglot Intelligence — P42.

Gives HxNexus the vocabulary of every major BPM tool.

Three functions:
    translate(tool, concept, session)  — find the Helix equivalent
    analyze(tool, text, session)       — parse a raw BPM config fragment
    compare(tool, question, session)   — side-by-side tool vs Helix explanation

All fall back to keyword matching when LLM is unavailable.
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import BpmConceptModel

logger = logging.getLogger(__name__)

_TRANSLATE_SYSTEM = (
    "You are HxNexus, an expert in enterprise BPM platforms. "
    "Given a concept from {tool}, explain exactly what it does and what the "
    "Velaris BPM equivalent is. Be concrete — name the Velaris table, step type, "
    "or module. If there is no direct equivalent, say so and suggest the closest approach."
)

_ANALYZE_SYSTEM = (
    "You are HxNexus, reverse-engineering a {tool} application for migration to Velaris BPM. "
    "Analyse the provided configuration fragment. Return:\n"
    "1. What this rule/element does in plain English\n"
    "2. The Velaris equivalent (be specific: case_type, stage, step type, form, routing rule, etc.)\n"
    "3. Confidence: exact / close / partial / manual\n"
    "4. Any caveats or things that need manual review\n"
    "Be concise and structured."
)

_COMPARE_SYSTEM = (
    "You are HxNexus, an expert in both {tool} and Velaris BPM. "
    "Answer the question by comparing how {tool} approaches it vs how Velaris approaches it. "
    "Format as two clear paragraphs: '{tool} approach:' then 'Velaris approach:'. "
    "Be specific — name actual {tool} constructs and their Velaris counterparts."
)


# ── Fuzzy concept matching (no LLM, works offline) ───────────────────────────

def _fuzzy_score(a: str, b: str) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return SequenceMatcher(None, a, b).ratio()


async def _find_concepts(
    session: AsyncSession,
    tool: str | None,
    concept: str,
    top_k: int = 3,
) -> list[BpmConceptModel]:
    """Return the top-k matching concepts from the knowledge base."""
    stmt = select(BpmConceptModel)
    if tool:
        stmt = stmt.where(BpmConceptModel.source_tool == tool.lower())
    rows = (await session.execute(stmt)).scalars().all()

    scored = sorted(rows, key=lambda r: _fuzzy_score(concept, r.source_concept), reverse=True)
    return scored[:top_k]


def _concept_to_dict(c: BpmConceptModel) -> dict:
    return {
        "id":              str(c.id),
        "source_tool":     c.source_tool,
        "source_concept":  c.source_concept,
        "helix_equiv":     c.helix_equiv,
        "helix_node_type": c.helix_node_type,
        "description":     c.description,
        "example":         c.example,
        "confidence":      c.confidence,
        "notes":           c.notes,
    }


# ── translate ─────────────────────────────────────────────────────────────────

async def translate(
    tool: str,
    concept: str,
    session: AsyncSession,
    llm=None,
) -> dict:
    """Translate a BPM concept to its Helix equivalent.

    1. Checks bpm_concepts table first (fast, no LLM).
    2. If exact/close match found → return it with optional LLM enrichment.
    3. If no match → LLM generates the mapping (fallback: 'unknown').
    """
    matches = await _find_concepts(session, tool, concept, top_k=1)
    best = matches[0] if matches else None
    best_score = _fuzzy_score(concept, best.source_concept) if best else 0.0

    # High-confidence DB match — return immediately, optionally enrich
    if best and best_score >= 0.75:
        result = _concept_to_dict(best)
        result["match_score"] = round(best_score, 3)
        result["source"] = "knowledge_base"

        # Enrich with LLM explanation if available (non-blocking)
        if llm is None:
            try:
                from case_service.hxnexus.factory import get_llm_backend
                llm = get_llm_backend()
            except Exception:
                pass

        if llm and getattr(llm, "available", False):
            try:
                prompt = (
                    f"In {tool}, '{concept}' maps to Velaris '{best.helix_equiv}'. "
                    f"Give a 2-sentence plain-English explanation of this mapping, "
                    f"with one concrete example."
                )
                enrichment = await llm.complete(
                    prompt,
                    system=_TRANSLATE_SYSTEM.format(tool=tool),
                    temperature=0.2,
                )
                if enrichment:
                    result["enrichment"] = enrichment.strip()
            except Exception as e:
                logger.debug("polyglot.translate: LLM enrichment skipped: %s", e)

        return result

    # No KB match — try LLM
    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            pass

    if llm and getattr(llm, "available", False):
        try:
            prompt = f"What is '{concept}' in {tool}, and what is the Velaris BPM equivalent?"
            answer = await llm.complete(
                prompt,
                system=_TRANSLATE_SYSTEM.format(tool=tool),
                temperature=0.2,
            )
            return {
                "source_tool":    tool,
                "source_concept": concept,
                "helix_equiv":    "See explanation",
                "description":    answer or "No answer generated.",
                "confidence":     "partial",
                "source":         "llm",
                "match_score":    0.0,
            }
        except Exception as e:
            logger.warning("polyglot.translate: LLM failed: %s", e)

    # Full fallback — partial match if available
    if best and best_score >= 0.4:
        result = _concept_to_dict(best)
        result["match_score"] = round(best_score, 3)
        result["source"] = "knowledge_base_partial"
        result["note"] = f"Partial match for '{concept}' — review manually."
        return result

    return {
        "source_tool":    tool,
        "source_concept": concept,
        "helix_equiv":    "unknown",
        "description":    f"No mapping found for '{concept}' in the knowledge base. LLM unavailable.",
        "confidence":     "manual",
        "source":         "none",
        "match_score":    0.0,
    }


# ── analyze ───────────────────────────────────────────────────────────────────

async def analyze(
    tool: str,
    text: str,
    session: AsyncSession,
    llm=None,
) -> dict:
    """Analyse a raw BPM config fragment and map it to Velaris.

    Keyword scan first; LLM for deeper interpretation.
    """
    # Step 1: keyword scan across all concepts for this tool
    all_concepts = (await session.execute(
        select(BpmConceptModel).where(BpmConceptModel.source_tool == tool.lower())
    )).scalars().all()

    keyword_hits: list[dict] = []
    text_lower = text.lower()
    for c in all_concepts:
        if c.source_concept.lower() in text_lower:
            keyword_hits.append(_concept_to_dict(c))

    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            pass

    # Step 2: LLM deep analysis
    llm_analysis: str | None = None
    if llm and getattr(llm, "available", False):
        try:
            kb_context = ""
            if keyword_hits:
                lines = [f"- {h['source_concept']} → {h['helix_equiv']}" for h in keyword_hits[:5]]
                kb_context = "\nKnown mappings for context:\n" + "\n".join(lines)

            prompt = f"Analyse this {tool} configuration fragment:{kb_context}\n\n```\n{text[:3000]}\n```"
            llm_analysis = await llm.complete(
                prompt,
                system=_ANALYZE_SYSTEM.format(tool=tool),
                temperature=0.2,
            )
        except Exception as e:
            logger.warning("polyglot.analyze: LLM failed: %s", e)

    return {
        "tool":           tool,
        "keyword_hits":   keyword_hits,
        "llm_analysis":   llm_analysis or (
            "LLM unavailable. Keyword scan identified the concepts above. "
            "Manual review required for full interpretation."
        ),
        "hint":           f"{len(keyword_hits)} known {tool} concept(s) detected in this fragment.",
    }


# ── compare ───────────────────────────────────────────────────────────────────

async def compare(
    tool: str,
    question: str,
    session: AsyncSession,
    llm=None,
) -> dict:
    """Answer a 'how would I do X in Helix vs {tool}?' question."""
    # Find related KB entries as grounding context
    related = await _find_concepts(session, tool, question, top_k=3)
    kb_context = "\n".join(
        f"- {c.source_concept} → {c.helix_equiv}: {c.description}"
        for c in related
        if _fuzzy_score(question, c.source_concept) > 0.3
    )

    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            pass

    answer: str
    if llm and getattr(llm, "available", False):
        try:
            prompt = f"Context from knowledge base:\n{kb_context}\n\nQuestion: {question}" if kb_context else question
            answer = await llm.complete(
                prompt,
                system=_COMPARE_SYSTEM.format(tool=tool),
                temperature=0.3,
            ) or "No answer generated."
        except Exception as e:
            answer = f"LLM error: {e}"
    else:
        if related and kb_context:
            answer = (
                f"{tool} approach: Uses {related[0].source_concept}.\n\n"
                f"Velaris approach: Uses {related[0].helix_equiv}. {related[0].description}"
            )
        else:
            answer = (
                f"LLM unavailable. No direct KB match found for this question. "
                f"Try a more specific {tool} concept name."
            )

    return {
        "tool":            tool,
        "question":        question,
        "answer":          answer,
        "related_concepts": [_concept_to_dict(c) for c in related],
    }
