"""P41 HxGraph — Helix Native Knowledge Graph.

Replaces graphify with a live, semantic, HxNexus-controlled knowledge graph
that covers the entire platform: business concepts, code structure, and
runtime patterns.

Public API:
    sync_graph(session)            — re-index all nodes from DB + code
    query_graph(question, session) — HxNexus-powered natural language query
    graph_report(session)          — graphify-equivalent markdown report
"""
from case_service.hxgraph.sync import sync_graph
from case_service.hxgraph.query import query_graph, path_between, explain_node, similar_nodes, impact_nodes
from case_service.hxgraph.report import graph_report, graph_export

__all__ = [
    "sync_graph",
    "query_graph", "path_between", "explain_node", "similar_nodes", "impact_nodes",
    "graph_report", "graph_export",
]
