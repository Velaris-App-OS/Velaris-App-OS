"""P41 HxGraph — Velaris Native Knowledge Graph.

Replaces graphify with a live, semantic, HxNexus-controlled knowledge graph
that covers the entire platform: business concepts, code structure, and
runtime patterns.

Public API:
    sync_graph(session)            — re-index all nodes from DB + code
    query_graph(question, session) — HxNexus-powered natural language query
    graph_report(session)          — graphify-equivalent markdown report
"""
from velaris_shared.hxgraph.sync import sync_graph
from velaris_shared.hxgraph.query import query_graph, path_between, explain_node, similar_nodes, impact_nodes
from velaris_shared.hxgraph.report import graph_report, graph_export

__all__ = [
    "sync_graph",
    "query_graph", "path_between", "explain_node", "similar_nodes", "impact_nodes",
    "graph_report", "graph_export",
]
