"""HxGraph — Knowledge Graph API (P41).

Endpoints:
  POST /graph/sync                     re-index all nodes from DB + code
  GET  /graph/nodes                    list/search nodes
  GET  /graph/nodes/{id}               node detail + neighbours
  GET  /graph/nodes/{id}/impact        what depends on this node
  GET  /graph/path                     BFS shortest path between two nodes
  GET  /graph/explain                  explain a concept (NL)
  GET  /graph/similar                  semantic similarity search
  POST /graph/query                    HxNexus natural language query
  GET  /graph/report                   graphify-equivalent markdown report
  GET  /graph/export                   graph.json export
  GET  /graph/visualize                interactive D3.js graph.html
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import GraphNodeModel, GraphEdgeModel
from case_service.db.session import get_session
from case_service.hxgraph.sync import sync_graph
from case_service.hxgraph.embedder import embed_nodes, build_similarity_edges, generate_summaries
from case_service.hxgraph.query import (
    query_graph, path_between, explain_node, similar_nodes, impact_nodes, _node_dict,
)
from case_service.hxgraph.report import graph_report, graph_export, graph_html

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/graph", tags=["hxgraph"])


# ── Sync ──────────────────────────────────────────────────────────────────────

@router.post("/sync")
async def trigger_sync(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Re-index all nodes from DB + Studio modules. Then embed + community detect."""
    stats = await sync_graph(session)

    # Best-effort: embed + similarity (skip if LLM unavailable)
    try:
        embedded = await embed_nodes(session)
        await generate_summaries(session)
        if embedded:
            await build_similarity_edges(session)
        stats["embedded"] = embedded
    except Exception as e:
        logger.warning("HxGraph: post-sync enrichment skipped: %s", e)
        stats["embedded"] = 0

    # Community detection + persist
    try:
        from case_service.hxgraph.community import detect_communities
        nodes = (await session.execute(select(GraphNodeModel))).scalars().all()
        edges = (await session.execute(select(GraphEdgeModel))).scalars().all()
        node_ids = [str(n.id) for n in nodes]
        edge_pairs = [(str(e.from_node_id), str(e.to_node_id)) for e in edges]
        community_map = detect_communities(node_ids, edge_pairs)
        id_to_node = {str(n.id): n for n in nodes}
        for nid, cid in community_map.items():
            if nid in id_to_node:
                id_to_node[nid].community_id = cid
        await session.commit()
        stats["communities"] = len(set(community_map.values()))
    except Exception as e:
        logger.warning("HxGraph: community detection skipped: %s", e)
        stats["communities"] = 0

    return stats


# ── Nodes ─────────────────────────────────────────────────────────────────────

@router.get("/nodes")
async def list_nodes(
    node_type: str | None = Query(None),
    q: str | None = Query(None, description="Label search"),
    limit: int = Query(100, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    stmt = select(GraphNodeModel)
    if node_type:
        stmt = stmt.where(GraphNodeModel.node_type == node_type)
    if q:
        stmt = stmt.where(GraphNodeModel.label.ilike(f"%{q}%"))
    stmt = stmt.limit(limit)
    nodes = (await session.execute(stmt)).scalars().all()
    return {"nodes": [_node_dict(n) for n in nodes], "total": len(nodes)}


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.hxgraph.query import _find_node
    node = await _find_node(session, node_id)
    if not node:
        raise HTTPException(404, f"Node '{node_id}' not found")

    outgoing = (await session.execute(
        select(GraphEdgeModel).where(GraphEdgeModel.from_node_id == node.id)
    )).scalars().all()
    incoming = (await session.execute(
        select(GraphEdgeModel).where(GraphEdgeModel.to_node_id == node.id)
    )).scalars().all()

    async def _nb(edge_id, is_outgoing):
        nid = edge_id
        nb = await session.get(GraphNodeModel, nid)
        return nb

    out_nbs = []
    for e in outgoing:
        nb = await session.get(GraphNodeModel, e.to_node_id)
        if nb:
            out_nbs.append({**_node_dict(nb), "edge_type": e.edge_type, "weight": e.weight})

    in_nbs = []
    for e in incoming:
        nb = await session.get(GraphNodeModel, e.from_node_id)
        if nb:
            in_nbs.append({**_node_dict(nb), "edge_type": e.edge_type, "weight": e.weight})

    return {
        **_node_dict(node),
        "outgoing": out_nbs,
        "incoming": in_nbs,
    }


@router.get("/nodes/{node_id}/impact")
async def get_impact(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await impact_nodes(session, node_id)
    if not result and not await _node_exists(session, node_id):
        raise HTTPException(404, f"Node '{node_id}' not found")
    return {"node_id": node_id, "depends_on_this": result, "count": len(result)}


# ── Traversal ─────────────────────────────────────────────────────────────────

@router.get("/path")
async def get_path(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    path = await path_between(session, from_, to)
    if path is None:
        return {"found": False, "from": from_, "to": to, "path": []}
    return {"found": True, "from": from_, "to": to, "length": len(path), "path": path}


@router.get("/explain")
async def get_explain(
    concept: str = Query(...),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await explain_node(session, concept)


@router.get("/similar")
async def get_similar(
    concept: str = Query(...),
    top_k: int = Query(8, le=20),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    results = await similar_nodes(session, concept, top_k=top_k)
    return {"concept": concept, "results": results}


class NLQuery(BaseModel):
    question: str

@router.post("/query")
async def nl_query(
    body: NLQuery,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await query_graph(session, body.question)


# ── Report + Export + Visualize ───────────────────────────────────────────────

@router.get("/report", response_class=PlainTextResponse)
async def get_report(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Graphify-equivalent GRAPH_REPORT.md in markdown."""
    md = await graph_report(session)
    return PlainTextResponse(content=md, media_type="text/markdown")


@router.get("/export")
async def get_export(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """graph.json equivalent — all nodes and edges as JSON."""
    return await graph_export(session)


@router.get("/visualize", response_class=HTMLResponse)
async def get_visualize(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """graph.html equivalent — interactive D3.js force-directed visualizer."""
    html = await graph_html(session)
    return HTMLResponse(content=html)


# ── Utils ─────────────────────────────────────────────────────────────────────

async def _node_exists(session: AsyncSession, node_id: str) -> bool:
    from case_service.hxgraph.query import _find_node
    return bool(await _find_node(session, node_id))
