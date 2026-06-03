"""HxGraph — Query engine: BFS path, impact, semantic search, NL query via HxNexus."""
from __future__ import annotations

import logging
from collections import deque

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import GraphNodeModel, GraphEdgeModel
from case_service.hxgraph.embedder import _cosine

logger = logging.getLogger(__name__)

_NL_SYSTEM = (
    "You are HxNexus answering questions about the HELIX BPM platform's knowledge graph. "
    "Use the provided graph context (nodes and their relationships) to give a clear, "
    "accurate answer. Be concise. If the answer is not in the context, say so."
)


# ── Node lookup ───────────────────────────────────────────────────────────────

async def _find_node(session: AsyncSession, identifier: str) -> GraphNodeModel | None:
    """Find a node by id, name, or label (fuzzy)."""
    # Try exact id
    try:
        import uuid as _uuid
        uid = _uuid.UUID(identifier)
        node = await session.get(GraphNodeModel, uid)
        if node:
            return node
    except (ValueError, AttributeError):
        pass

    # Try exact name
    node = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.name == identifier)
    )).scalar_one_or_none()
    if node:
        return node

    # Try label ILIKE
    nodes = (await session.execute(
        select(GraphNodeModel).where(
            GraphNodeModel.label.ilike(f"%{identifier}%")
        ).limit(1)
    )).scalars().all()
    return nodes[0] if nodes else None


# ── BFS path ─────────────────────────────────────────────────────────────────

async def path_between(
    session: AsyncSession,
    from_id: str,
    to_id: str,
    max_depth: int = 6,
) -> list[dict] | None:
    """BFS shortest path between two nodes. Returns ordered list of nodes, or None."""
    start = await _find_node(session, from_id)
    end   = await _find_node(session, to_id)
    if not start or not end:
        return None
    if start.id == end.id:
        return [_node_dict(start)]

    visited = {start.id}
    queue: deque[list[GraphNodeModel]] = deque([[start]])

    while queue:
        path = queue.popleft()
        current = path[-1]
        if len(path) > max_depth:
            continue

        edges = (await session.execute(
            select(GraphEdgeModel).where(GraphEdgeModel.from_node_id == current.id)
        )).scalars().all()

        for edge in edges:
            if edge.to_node_id in visited:
                continue
            neighbour = await session.get(GraphNodeModel, edge.to_node_id)
            if not neighbour:
                continue
            new_path = path + [neighbour]
            if neighbour.id == end.id:
                return [_node_dict(n) for n in new_path]
            visited.add(neighbour.id)
            queue.append(new_path)

    return None  # No path found


# ── Impact analysis ───────────────────────────────────────────────────────────

async def impact_nodes(
    session: AsyncSession,
    node_id: str,
    max_depth: int = 4,
) -> list[dict]:
    """Return all nodes that depend on (have edges pointing to) this node."""
    target = await _find_node(session, node_id)
    if not target:
        return []

    visited = {target.id}
    result: list[dict] = []
    queue: deque[GraphNodeModel] = deque([target])
    depth = 0

    while queue and depth < max_depth:
        level_size = len(queue)
        for _ in range(level_size):
            node = queue.popleft()
            incoming = (await session.execute(
                select(GraphEdgeModel).where(GraphEdgeModel.to_node_id == node.id)
            )).scalars().all()
            for edge in incoming:
                if edge.from_node_id in visited:
                    continue
                upstream = await session.get(GraphNodeModel, edge.from_node_id)
                if upstream:
                    visited.add(upstream.id)
                    result.append({**_node_dict(upstream), "edge_type": edge.edge_type})
                    queue.append(upstream)
        depth += 1

    return result


# ── Semantic similar nodes ────────────────────────────────────────────────────

async def similar_nodes(
    session: AsyncSession,
    concept: str,
    top_k: int = 8,
    llm=None,
) -> list[dict]:
    """Find nodes semantically similar to a free-text concept."""
    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            llm = None

    embedding: list[float] = []
    if llm and getattr(llm, "available", False):
        try:
            embedding = await llm.embed(concept)
        except Exception:
            pass

    if not embedding:
        # Fall back to label text search
        nodes = (await session.execute(
            select(GraphNodeModel).where(
                GraphNodeModel.label.ilike(f"%{concept}%")
            ).limit(top_k)
        )).scalars().all()
        return [_node_dict(n) for n in nodes]

    all_nodes = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.embedding.isnot(None))
    )).scalars().all()

    scored = [
        (n, _cosine(embedding, n.embedding or []))
        for n in all_nodes
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [
        {**_node_dict(n), "similarity": round(score, 4)}
        for n, score in scored[:top_k]
        if score > 0.4
    ]


# ── Explain (single node) ─────────────────────────────────────────────────────

async def explain_node(
    session: AsyncSession,
    concept: str,
    llm=None,
) -> dict:
    """Find the node matching concept and explain it with its neighbourhood."""
    node = await _find_node(session, concept)
    if not node:
        sims = await similar_nodes(session, concept, top_k=1, llm=llm)
        if sims:
            node = await session.get(GraphNodeModel, sims[0]["id"])
    if not node:
        return {"error": f"No node found for '{concept}'"}

    # Get neighbourhood
    outgoing = (await session.execute(
        select(GraphEdgeModel).where(GraphEdgeModel.from_node_id == node.id)
    )).scalars().all()
    incoming = (await session.execute(
        select(GraphEdgeModel).where(GraphEdgeModel.to_node_id == node.id)
    )).scalars().all()

    nb_labels = []
    for edge in (outgoing + incoming):
        nb_id = edge.to_node_id if edge.from_node_id == node.id else edge.from_node_id
        nb = await session.get(GraphNodeModel, nb_id)
        if nb:
            nb_labels.append(f"{edge.edge_type}: {nb.label}")

    context = f"Node: {node.label} ({node.node_type})\nProperties: {node.properties}\nNeighbours: {', '.join(nb_labels[:10])}"

    explanation = node.summary or f"{node.label} is a {node.node_type} in the Helix platform."

    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            pass

    if llm and getattr(llm, "available", False):
        try:
            prompt = f"Explain this platform component and its relationships:\n{context}"
            explanation = await llm.complete(prompt, system=_NL_SYSTEM, temperature=0.3)
        except Exception:
            pass

    return {
        **_node_dict(node),
        "explanation": explanation,
        "neighbours": nb_labels[:15],
    }


# ── Natural language query ────────────────────────────────────────────────────

async def query_graph(
    session: AsyncSession,
    question: str,
    llm=None,
    top_k: int = 8,
) -> dict:
    """HxNexus-powered natural language query over the graph."""
    if llm is None:
        try:
            from case_service.hxnexus.factory import get_llm_backend
            llm = get_llm_backend()
        except Exception:
            llm = None

    # Find semantically relevant nodes
    relevant = await similar_nodes(session, question, top_k=top_k, llm=llm)

    context_parts = [f"Graph has {len(relevant)} relevant nodes for this query:"]
    for r in relevant:
        summary = r.get("summary") or ""
        context_parts.append(f"- [{r['node_type']}] {r['label']}: {summary}")

    context = "\n".join(context_parts)
    prompt = f"Context:\n{context}\n\nQuestion: {question}"

    answer = "Graph query requires an LLM backend (Ollama/OpenAI/Anthropic). Configure HxNexus to enable natural language queries."
    if llm and getattr(llm, "available", False):
        try:
            answer = await llm.complete(prompt, system=_NL_SYSTEM, temperature=0.3)
        except Exception as e:
            answer = f"LLM error: {e}"

    return {
        "question": question,
        "answer": answer,
        "relevant_nodes": relevant,
    }


# ── Utils ─────────────────────────────────────────────────────────────────────

def _node_dict(node: GraphNodeModel) -> dict:
    return {
        "id": str(node.id),
        "node_type": node.node_type,
        "name": node.name,
        "label": node.label,
        "summary": node.summary,
        "community_id": node.community_id,
        "properties": node.properties or {},
    }
