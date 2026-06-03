"""HxGraph — Embedding and semantic similarity wiring.

Uses the same LLM embed() + cosine pattern as HxNexus DbVectorStore.
Computes similar_to edges between nodes whose cosine similarity exceeds threshold.
"""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import GraphNodeModel, GraphEdgeModel
from case_service.hxgraph.sync import _upsert_edge

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.72
MAX_SIMILAR_EDGES_PER_NODE = 8


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    try:
        import numpy as np
        va = np.array(a, dtype=np.float32)
        vb = np.array(b, dtype=np.float32)
        denom = np.linalg.norm(va) * np.linalg.norm(vb)
        return float(np.dot(va, vb) / denom) if denom else 0.0
    except Exception:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0


def _node_text(node: GraphNodeModel) -> str:
    """Build the text representation embedded for a node."""
    props = node.properties or {}
    parts = [f"{node.node_type}: {node.label}"]
    if desc := props.get("description"):
        parts.append(desc)
    if node.summary:
        parts.append(node.summary)
    return " | ".join(parts)


async def embed_nodes(session: AsyncSession, llm=None) -> int:
    """Embed all nodes that have no embedding yet. Returns count embedded."""
    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            logger.warning("HxGraph: no LLM backend — skipping embedding")
            return 0

    if not getattr(llm, "available", False):
        logger.info("HxGraph: LLM not available — skipping embedding")
        return 0

    nodes = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.embedding.is_(None))
    )).scalars().all()

    count = 0
    for node in nodes:
        try:
            text = _node_text(node)
            embedding = await llm.embed(text)
            if embedding:
                node.embedding = embedding
                count += 1
        except Exception as e:
            logger.debug("HxGraph: embed failed for %s: %s", node.name, e)

    if count:
        await session.commit()
    logger.info("HxGraph: embedded %d nodes", count)
    return count


async def build_similarity_edges(session: AsyncSession) -> int:
    """Compute similar_to edges between nodes with cosine > threshold."""
    nodes = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.embedding.isnot(None))
    )).scalars().all()

    if len(nodes) < 2:
        return 0

    created = 0
    for i, a in enumerate(nodes):
        scores: list[tuple[float, GraphNodeModel]] = []
        for j, b in enumerate(nodes):
            if i == j:
                continue
            score = _cosine(a.embedding or [], b.embedding or [])
            if score >= SIMILARITY_THRESHOLD:
                scores.append((score, b))

        scores.sort(key=lambda x: x[0], reverse=True)
        for score, b in scores[:MAX_SIMILAR_EDGES_PER_NODE]:
            existing = (await session.execute(
                select(GraphEdgeModel).where(
                    GraphEdgeModel.from_node_id == a.id,
                    GraphEdgeModel.to_node_id == b.id,
                    GraphEdgeModel.edge_type == "similar_to",
                )
            )).scalar_one_or_none()
            if not existing:
                session.add(GraphEdgeModel(
                    from_node_id=a.id,
                    to_node_id=b.id,
                    edge_type="similar_to",
                    weight=round(score, 4),
                ))
                created += 1

    if created:
        await session.commit()
    logger.info("HxGraph: created %d similarity edges", created)
    return created


async def generate_summaries(session: AsyncSession, llm=None) -> int:
    """Ask HxNexus to write a 1-2 line summary for each node without one."""
    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            return 0

    if not getattr(llm, "available", False):
        return 0

    nodes = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.summary.is_(None))
    )).scalars().all()

    system = (
        "You are describing components of an enterprise BPM platform called Helix. "
        "Write a single concise sentence (max 20 words) describing what the given component does. "
        "Be specific and plain — no jargon."
    )

    count = 0
    for node in nodes:
        try:
            prompt = f"Describe this platform component in one sentence: {_node_text(node)}"
            summary = await llm.complete(prompt, system=system, temperature=0.2)
            if summary:
                node.summary = summary.strip()
                count += 1
        except Exception as e:
            logger.debug("HxGraph: summary failed for %s: %s", node.name, e)

    if count:
        await session.commit()
    logger.info("HxGraph: generated %d summaries", count)
    return count
