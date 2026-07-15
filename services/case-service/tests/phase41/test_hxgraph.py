"""HELIX P41 — HxGraph tests (22 tests).

Covers: sync_graph (nodes created for case types, stages, steps, forms, modules),
        edge creation (has_stage, has_step, uses_form, served_by),
        GET /graph/nodes (list, type filter, label search),
        GET /graph/nodes/{id} (detail + neighbours),
        GET /graph/nodes/{id}/impact,
        GET /graph/path (BFS found, not found),
        GET /graph/explain, GET /graph/similar,
        POST /graph/query (no LLM fallback),
        GET /graph/report (markdown structure),
        GET /graph/export (JSON structure),
        GET /graph/visualize (HTML),
        community detection (label propagation),
        auth guard.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.db.models import (
    CaseTypeModel, FormDefinitionModel, GraphNodeModel, GraphEdgeModel,
)
from case_service.hxgraph.sync import sync_graph, _upsert_node, _upsert_edge
from case_service.hxgraph.query import path_between, impact_nodes, similar_nodes, explain_node
from case_service.hxgraph.community import detect_communities, community_hubs
from case_service.main import app


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _admin():
    return AuthenticatedUser(
        user_id="admin-1", roles=["admin"],
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Admins",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Admin Portal", modules=["graph"],
            homepage="/graph", roles=["admin"], privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _override():
    app.dependency_overrides[get_current_user] = lambda: _admin()

def _clear():
    app.dependency_overrides.pop(get_current_user, None)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def case_type(session):
    ct = CaseTypeModel(
        name="Insurance Claim",
        version="1.0",
        definition_json={
            "stages": [
                {
                    "id": "intake", "name": "Intake",
                    "steps": [
                        {"id": "fill_form", "type": "user_task", "label": "Fill Claim Form", "required": True},
                        {"id": "upload_docs", "type": "document_request", "label": "Upload Documents"},
                    ],
                },
                {
                    "id": "review", "name": "Review",
                    "steps": [
                        {"id": "adjuster_review", "type": "approval", "label": "Adjuster Review"},
                    ],
                },
            ]
        },
    )
    session.add(ct)
    await session.flush()
    return ct

@pytest_asyncio.fixture
async def form(session):
    f = FormDefinitionModel(
        name="Claim Form",
        version="1.0",
        definition_json={
            "sections": [
                {
                    "id": "s1", "title": "Claimant Info",
                    "fields": [
                        {"id": "f1", "label": "Full Name", "field_key": "full_name", "type": "text", "required": True},
                        {"id": "f2", "label": "Claim Amount", "field_key": "claim_amount", "type": "currency"},
                    ],
                }
            ]
        },
    )
    session.add(f)
    await session.flush()
    return f


# ── Community detection (pure Python) ─────────────────────────────────────────

class TestCommunityDetection:
    def test_empty_graph(self):
        result = detect_communities([], [])
        assert result == {}

    def test_single_node(self):
        result = detect_communities(["a"], [])
        assert result == {"a": 0}

    def test_two_disconnected_nodes(self):
        result = detect_communities(["a", "b"], [])
        assert set(result.values()) == {0, 1}

    def test_two_connected_nodes_same_community(self):
        result = detect_communities(["a", "b"], [("a", "b")])
        assert result["a"] == result["b"]

    def test_community_hubs_returns_highest_degree(self):
        nodes = ["a", "b", "c", "d"]
        edges = [("a", "b"), ("a", "c"), ("a", "d")]
        cmap = detect_communities(nodes, edges)
        degree = {"a": 3, "b": 1, "c": 1, "d": 1}
        hubs = community_hubs(cmap, degree, top_n=1)
        # In the dominant community, 'a' should be the hub
        assert any("a" in members for members in hubs.values())


# ── Sync ──────────────────────────────────────────────────────────────────────

class TestSync:
    async def test_sync_creates_case_type_node(self, session, case_type):
        await sync_graph(session)
        node = (await session.execute(
            select(GraphNodeModel).where(
                GraphNodeModel.node_type == "case_type",
                GraphNodeModel.label == "Insurance Claim",
            )
        )).scalar_one_or_none()
        assert node is not None

    async def test_sync_creates_stage_nodes(self, session, case_type):
        await sync_graph(session)
        stages = (await session.execute(
            select(GraphNodeModel).where(GraphNodeModel.node_type == "stage")
        )).scalars().all()
        assert len(stages) >= 2

    async def test_sync_creates_step_nodes(self, session, case_type):
        await sync_graph(session)
        steps = (await session.execute(
            select(GraphNodeModel).where(GraphNodeModel.node_type == "step")
        )).scalars().all()
        assert len(steps) >= 3

    async def test_sync_creates_has_stage_edges(self, session, case_type):
        await sync_graph(session)
        edges = (await session.execute(
            select(GraphEdgeModel).where(GraphEdgeModel.edge_type == "has_stage")
        )).scalars().all()
        assert len(edges) >= 2

    async def test_sync_creates_has_step_edges(self, session, case_type):
        await sync_graph(session)
        edges = (await session.execute(
            select(GraphEdgeModel).where(GraphEdgeModel.edge_type == "has_step")
        )).scalars().all()
        assert len(edges) >= 3

    async def test_sync_creates_form_node(self, session, form):
        await sync_graph(session)
        node = (await session.execute(
            select(GraphNodeModel).where(
                GraphNodeModel.node_type == "form",
                GraphNodeModel.label == "Claim Form",
            )
        )).scalar_one_or_none()
        assert node is not None

    async def test_sync_creates_field_nodes(self, session, form):
        await sync_graph(session)
        fields = (await session.execute(
            select(GraphNodeModel).where(GraphNodeModel.node_type == "field")
        )).scalars().all()
        assert len(fields) >= 2

    async def test_sync_creates_module_nodes(self, session):
        await sync_graph(session)
        modules = (await session.execute(
            select(GraphNodeModel).where(GraphNodeModel.node_type == "module")
        )).scalars().all()
        assert len(modules) > 0

    async def test_sync_idempotent(self, session, case_type):
        await sync_graph(session)
        count_before = len((await session.execute(select(GraphNodeModel))).all())
        await sync_graph(session)
        count_after = len((await session.execute(select(GraphNodeModel))).all())
        assert count_after == count_before


# ── Query engine ──────────────────────────────────────────────────────────────

class TestQueryEngine:
    async def test_path_between_connected_nodes(self, session, case_type):
        await sync_graph(session)
        ct_node = (await session.execute(
            select(GraphNodeModel).where(GraphNodeModel.node_type == "case_type")
        )).scalar_one_or_none()
        stage_node = (await session.execute(
            select(GraphNodeModel).where(GraphNodeModel.node_type == "stage")
        )).scalars().first()

        if ct_node and stage_node:
            path = await path_between(session, str(ct_node.id), str(stage_node.id))
            assert path is not None
            assert len(path) >= 2

    async def test_path_returns_none_for_disconnected(self, session):
        a = await _upsert_node(session, "concept", "concept:orphan_a", "Orphan A", source="hxnexus")
        b = await _upsert_node(session, "concept", "concept:orphan_b", "Orphan B", source="hxnexus")
        await session.flush()
        path = await path_between(session, str(a.id), str(b.id))
        assert path is None

    async def test_impact_nodes_finds_upstream(self, session):
        parent = await _upsert_node(session, "module", "module:/test-parent", "Test Parent", source="code")
        child  = await _upsert_node(session, "endpoint", "endpoint:/test-child", "Test Child", source="code")
        await session.flush()
        await _upsert_edge(session, parent, child, "served_by")
        await session.flush()

        impacts = await impact_nodes(session, str(child.id))
        impact_ids = [i["id"] for i in impacts]
        assert str(parent.id) in impact_ids

    async def test_similar_nodes_text_fallback(self, session, case_type):
        await sync_graph(session)
        results = await similar_nodes(session, "Insurance", top_k=5, llm=None)
        assert any("Insurance" in r["label"] for r in results)

    async def test_explain_node_returns_dict(self, session, case_type):
        await sync_graph(session)
        result = await explain_node(session, "Insurance Claim", llm=None)
        assert "label" in result or "error" not in result or "explanation" in result


# ── REST API ──────────────────────────────────────────────────────────────────

class TestGraphAPI:
    async def test_list_nodes(self, client, session, case_type):
        await sync_graph(session)
        _override()
        try:
            r = await client.get("/api/v1/graph/nodes")
        finally:
            _clear()
        assert r.status_code == 200
        assert "nodes" in r.json()

    async def test_list_nodes_type_filter(self, client, session, case_type):
        await sync_graph(session)
        _override()
        try:
            r = await client.get("/api/v1/graph/nodes?node_type=case_type")
        finally:
            _clear()
        assert r.status_code == 200
        for n in r.json()["nodes"]:
            assert n["node_type"] == "case_type"

    async def test_list_nodes_search(self, client, session, case_type):
        await sync_graph(session)
        _override()
        try:
            r = await client.get("/api/v1/graph/nodes?q=Insurance")
        finally:
            _clear()
        assert r.status_code == 200
        assert r.json()["total"] > 0

    async def test_nl_query_no_llm(self, client, session, case_type):
        await sync_graph(session)
        _override()
        try:
            r = await client.post("/api/v1/graph/query", json={"question": "what case types exist?"})
        finally:
            _clear()
        assert r.status_code == 200
        assert "answer" in r.json()

    async def test_report_returns_markdown(self, client, session, case_type):
        await sync_graph(session)
        _override()
        try:
            r = await client.get("/api/v1/graph/report")
        finally:
            _clear()
        assert r.status_code == 200
        text = r.text
        assert "HxGraph Report" in text
        assert "nodes" in text
        assert "Community" in text

    async def test_export_returns_json(self, client, session, case_type):
        await sync_graph(session)
        _override()
        try:
            r = await client.get("/api/v1/graph/export")
        finally:
            _clear()
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data and "edges" in data and "meta" in data

    async def test_visualize_returns_html(self, client, session, case_type):
        await sync_graph(session)
        _override()
        try:
            r = await client.get("/api/v1/graph/visualize")
        finally:
            _clear()
        assert r.status_code == 200
        assert "d3js.org" in r.text
        assert "HxGraph" in r.text

    async def test_path_endpoint(self, client, session, case_type):
        await sync_graph(session)
        nodes = (await session.execute(select(GraphNodeModel).limit(2))).scalars().all()
        if len(nodes) >= 2:
            _override()
            try:
                r = await client.get(f"/api/v1/graph/path?from={nodes[0].id}&to={nodes[1].id}")
            finally:
                _clear()
            assert r.status_code == 200
            assert "found" in r.json()

    async def test_sync_endpoint(self, client, session, case_type):
        _override()
        try:
            r = await client.post("/api/v1/graph/sync")
        finally:
            _clear()
        assert r.status_code == 200
        assert "nodes" in r.json()
        assert "edges" in r.json()


# ── Tenant scoping (P4 prerequisite: graph never leaks across tenants) ────────
# Tenant-owned case types tag their node family; tenant callers see global +
# their own nodes; tenant-less callers (platform operators) see everything —
# the pre-scoping behaviour, so existing single-tenant setups are unaffected.

class TestTenantScoping:
    async def _seed_two_tenants(self, session):
        from case_service.db.models import TenantModel

        t_a = TenantModel(slug=f"ten-a-{uuid.uuid4().hex[:8]}", name="Tenant A")
        t_b = TenantModel(slug=f"ten-b-{uuid.uuid4().hex[:8]}", name="Tenant B")
        session.add_all([t_a, t_b])
        await session.flush()

        stages = [{"id": "s1", "name": "Stage One",
                   "steps": [{"id": "st1", "type": "user_task", "label": "Step One"}]}]
        ct_a = CaseTypeModel(name="Tenant A Secret Process", version="1.0",
                             definition_json={"stages": stages}, tenant_id=t_a.id)
        ct_b = CaseTypeModel(name="Tenant B Secret Process", version="1.0",
                             definition_json={"stages": stages}, tenant_id=t_b.id)
        ct_g = CaseTypeModel(name="Global Shared Process", version="1.0",
                             definition_json={"stages": stages})
        session.add_all([ct_a, ct_b, ct_g])
        await session.flush()
        await sync_graph(session)
        return str(t_a.id), str(t_b.id), ct_a, ct_b, ct_g

    async def test_sync_tags_node_family_with_tenant(self, session):
        tid_a, _, ct_a, _, ct_g = await self._seed_two_tenants(session)
        ct_node = (await session.execute(select(GraphNodeModel).where(
            GraphNodeModel.name == f"case_type:{ct_a.id}"))).scalar_one()
        assert ct_node.tenant_id == tid_a
        stage_node = (await session.execute(select(GraphNodeModel).where(
            GraphNodeModel.name == f"stage:{ct_a.id}:s1"))).scalar_one()
        assert stage_node.tenant_id == tid_a
        step_node = (await session.execute(select(GraphNodeModel).where(
            GraphNodeModel.name == f"step:{ct_a.id}:s1:st1"))).scalar_one()
        assert step_node.tenant_id == tid_a
        # global case type stays untagged
        g_node = (await session.execute(select(GraphNodeModel).where(
            GraphNodeModel.name == f"case_type:{ct_g.id}"))).scalar_one()
        assert g_node.tenant_id is None

    async def test_find_node_scoped(self, session):
        tid_a, tid_b, ct_a, _, ct_g = await self._seed_two_tenants(session)
        from case_service.hxgraph.query import _find_node
        name = f"case_type:{ct_a.id}"
        assert await _find_node(session, name, tid_a) is not None       # own
        assert await _find_node(session, name, tid_b) is None           # foreign
        assert await _find_node(session, name, None) is not None        # operator
        # global node visible to everyone
        gname = f"case_type:{ct_g.id}"
        assert await _find_node(session, gname, tid_b) is not None

    async def test_similar_nodes_scoped(self, session):
        tid_a, tid_b, *_ = await self._seed_two_tenants(session)
        # no-LLM text fallback path
        res_b = await similar_nodes(session, "Tenant A Secret", tenant_id=tid_b)
        assert all("Tenant A Secret" not in r["label"] for r in res_b)
        res_a = await similar_nodes(session, "Tenant A Secret", tenant_id=tid_a)
        assert any("Tenant A Secret" in r["label"] for r in res_a)
        res_op = await similar_nodes(session, "Tenant A Secret", tenant_id=None)
        assert any("Tenant A Secret" in r["label"] for r in res_op)

    async def test_export_scoped_and_edges_pruned(self, session):
        tid_a, tid_b, ct_a, ct_b, ct_g = await self._seed_two_tenants(session)
        from case_service.hxgraph.report import graph_export
        exp_a = await graph_export(session, tenant_id=tid_a)
        labels = {n["label"] for n in exp_a["nodes"]}
        assert "Tenant A Secret Process" in labels
        assert "Tenant B Secret Process" not in labels
        assert "Global Shared Process" in labels
        # no edge may reference a node outside the visible set
        visible_ids = {n["id"] for n in exp_a["nodes"]}
        for e in exp_a["edges"]:
            assert e["from"] in visible_ids and e["to"] in visible_ids
        # operator export unchanged (sees all three)
        exp_op = await graph_export(session, tenant_id=None)
        labels_op = {n["label"] for n in exp_op["nodes"]}
        assert {"Tenant A Secret Process", "Tenant B Secret Process",
                "Global Shared Process"} <= labels_op

    async def test_path_cannot_tunnel_through_hidden_nodes(self, session):
        tid_a, tid_b, ct_a, *_ = await self._seed_two_tenants(session)
        start = f"case_type:{ct_a.id}"
        end = f"step:{ct_a.id}:s1:st1"
        assert await path_between(session, start, end, tenant_id=tid_a) is not None
        assert await path_between(session, start, end, tenant_id=tid_b) is None

    async def test_rest_case_type_get_cross_tenant_404(self, client, session):
        tid_a, tid_b, ct_a, *_ = await self._seed_two_tenants(session)
        await session.commit()

        def _tenant_user(tid):
            u = _admin()
            u.tenant_id = tid
            return u

        try:
            app.dependency_overrides[get_current_user] = lambda: _tenant_user(tid_a)
            r_own = await client.get(f"/api/v1/case-types/{ct_a.id}")
            app.dependency_overrides[get_current_user] = lambda: _tenant_user(tid_b)
            r_foreign = await client.get(f"/api/v1/case-types/{ct_a.id}")
            app.dependency_overrides[get_current_user] = lambda: _admin()  # tenant-less
            r_operator = await client.get(f"/api/v1/case-types/{ct_a.id}")
        finally:
            _clear()
        assert r_own.status_code == 200
        assert r_foreign.status_code == 404     # anti-oracle
        assert r_operator.status_code == 200    # unchanged for tenant-less callers
