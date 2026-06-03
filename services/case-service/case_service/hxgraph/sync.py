"""HxGraph — DB + code scanner that populates graph_nodes and graph_edges.

Run on startup (background task) or via POST /graph/sync.
Idempotent: existing nodes are updated in-place (upsert by name).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from case_service.db.models import (
    GraphNodeModel, GraphEdgeModel,
    CaseTypeModel, FormDefinitionModel, AccessGroupModel, AccessRoleModel, TenantModel,
)
from case_service.api.routers.sitemap import MODULES

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _upsert_node(
    session: AsyncSession,
    node_type: str,
    name: str,
    label: str,
    source: str = "db",
    properties: dict | None = None,
    tenant_id: str | None = None,
) -> GraphNodeModel:
    """Upsert a node by (node_type, name). Returns the model instance."""
    existing = (await session.execute(
        select(GraphNodeModel).where(
            GraphNodeModel.node_type == node_type,
            GraphNodeModel.name == name,
        )
    )).scalar_one_or_none()

    if existing:
        existing.label = label
        existing.properties = properties or {}
        existing.source = source
        existing.last_synced_at = _utcnow()
        if tenant_id:
            existing.tenant_id = tenant_id
        return existing

    node = GraphNodeModel(
        node_type=node_type,
        name=name,
        label=label,
        source=source,
        properties=properties or {},
        tenant_id=tenant_id,
    )
    session.add(node)
    await session.flush()
    return node


async def _upsert_edge(
    session: AsyncSession,
    from_node: GraphNodeModel,
    to_node: GraphNodeModel,
    edge_type: str,
    weight: float = 1.0,
    properties: dict | None = None,
) -> None:
    existing = (await session.execute(
        select(GraphEdgeModel).where(
            GraphEdgeModel.from_node_id == from_node.id,
            GraphEdgeModel.to_node_id == to_node.id,
            GraphEdgeModel.edge_type == edge_type,
        )
    )).scalar_one_or_none()

    if not existing:
        session.add(GraphEdgeModel(
            from_node_id=from_node.id,
            to_node_id=to_node.id,
            edge_type=edge_type,
            weight=weight,
            properties=properties or {},
        ))


# ── Sync: Case Types → Stages → Steps ────────────────────────────────────────

async def _sync_case_types(session: AsyncSession) -> None:
    case_types = (await session.execute(select(CaseTypeModel))).scalars().all()

    for ct in case_types:
        defn = ct.definition_json or {}
        stages = defn.get("stages", [])

        ct_node = await _upsert_node(
            session, "case_type",
            name=f"case_type:{ct.id}",
            label=ct.name,
            properties={
                "id": str(ct.id),
                "slug": getattr(ct, "slug", ""),
                "stage_count": len(stages),
                "version": ct.version,
                "description": ct.description or "",
            },
        )

        for stage in stages:
            stage_id = stage.get("id", "")
            stage_label = stage.get("name", stage_id)
            steps = stage.get("steps", [])

            stage_node = await _upsert_node(
                session, "stage",
                name=f"stage:{ct.id}:{stage_id}",
                label=f"{ct.name} → {stage_label}",
                properties={
                    "stage_id": stage_id,
                    "case_type_id": str(ct.id),
                    "step_count": len(steps),
                    "sla_hours": stage.get("sla_hours"),
                },
            )
            await _upsert_edge(session, ct_node, stage_node, "has_stage")

            for step in steps:
                step_id = step.get("id", "")
                step_type = step.get("type", "user_task")
                step_label = step.get("label", step_id)

                step_node = await _upsert_node(
                    session, "step",
                    name=f"step:{ct.id}:{stage_id}:{step_id}",
                    label=f"{stage_label} → {step_label}",
                    properties={
                        "step_id": step_id,
                        "step_type": step_type,
                        "stage_id": stage_id,
                        "case_type_id": str(ct.id),
                        "form_id": step.get("form_id"),
                        "required": step.get("required", True),
                    },
                )
                await _upsert_edge(session, stage_node, step_node, "has_step")


# ── Sync: Forms → Fields ──────────────────────────────────────────────────────

async def _sync_forms(session: AsyncSession) -> None:
    forms = (await session.execute(select(FormDefinitionModel))).scalars().all()

    for form in forms:
        defn = form.definition_json or {}
        sections = defn.get("sections", [])
        field_count = sum(len(s.get("fields", [])) for s in sections)

        form_node = await _upsert_node(
            session, "form",
            name=f"form:{form.id}",
            label=form.name,
            properties={
                "id": str(form.id),
                "version": form.version,
                "section_count": len(sections),
                "field_count": field_count,
            },
        )

        for section in sections:
            for field in section.get("fields", []):
                fid = field.get("id", "")
                field_node = await _upsert_node(
                    session, "field",
                    name=f"field:{form.id}:{fid}",
                    label=f"{form.name} → {field.get('label', fid)}",
                    properties={
                        "field_id": fid,
                        "field_key": field.get("field_key", fid),
                        "field_type": field.get("type", "text"),
                        "required": field.get("required", False),
                        "form_id": str(form.id),
                    },
                )
                await _upsert_edge(session, form_node, field_node, "has_field")

        # Wire steps that use this form (filter in Python — avoids JSONB-only operators)
        all_steps = (await session.execute(
            select(GraphNodeModel).where(GraphNodeModel.node_type == "step")
        )).scalars().all()
        for step_node in all_steps:
            if (step_node.properties or {}).get("form_id") == str(form.id):
                await _upsert_edge(session, step_node, form_node, "uses_form")


# ── Sync: Access Groups ───────────────────────────────────────────────────────

async def _sync_access_groups(session: AsyncSession) -> None:
    groups = (await session.execute(select(AccessGroupModel))).scalars().all()
    for ag in groups:
        await _upsert_node(
            session, "access_group",
            name=f"access_group:{ag.id}",
            label=ag.name,
            properties={
                "id": str(ag.id),
                "tenant_id": ag.tenant_id or "",
                "description": ag.description or "",
                "is_default": ag.is_default,
            },
            tenant_id=ag.tenant_id,
        )


# ── Sync: Access Roles ───────────────────────────────────────────────────────

async def _sync_access_roles(session: AsyncSession) -> None:
    roles = (await session.execute(select(AccessRoleModel))).scalars().all()
    role_node_map: dict[str, GraphNodeModel] = {}

    for role in roles:
        node = await _upsert_node(
            session, "access_role",
            name=f"access_role:{role.id}",
            label=role.name,
            properties={
                "id": str(role.id),
                "tenant_id": role.tenant_id or "",
                "description": role.description or "",
                "privilege_count": len(role.privileges or []),
            },
            tenant_id=role.tenant_id,
        )
        role_node_map[str(role.id)] = node

    # Wire access groups → their roles
    groups = (await session.execute(select(AccessGroupModel))).scalars().all()
    for ag in groups:
        ag_node = (await session.execute(
            select(GraphNodeModel).where(
                GraphNodeModel.node_type == "access_group",
                GraphNodeModel.name == f"access_group:{ag.id}",
            )
        )).scalar_one_or_none()
        if not ag_node:
            continue
        for role_id in ag.role_ids or []:
            role_node = role_node_map.get(str(role_id))
            if role_node:
                await _upsert_edge(session, ag_node, role_node, "has_role")


# ── Sync: Studio Modules (from sitemap) ──────────────────────────────────────

async def _sync_modules(session: AsyncSession) -> None:
    for mod in MODULES:
        mod_node = await _upsert_node(
            session, "module",
            name=f"module:{mod['path']}",
            label=mod["label"],
            source="code",
            properties={
                "path": mod["path"],
                "category": mod.get("category", ""),
                "description": mod.get("description", ""),
                "phase": mod.get("phase"),
            },
        )

        # Create endpoint nodes for each API endpoint listed in the module
        for ep in mod.get("api_endpoints", []):
            ep_node = await _upsert_node(
                session, "endpoint",
                name=f"endpoint:{ep}",
                label=ep,
                source="code",
                properties={"path": ep, "module_path": mod["path"]},
            )
            await _upsert_edge(session, mod_node, ep_node, "served_by")


# ── Main entry point ──────────────────────────────────────────────────────────

async def sync_graph(session: AsyncSession) -> dict:
    """Full graph re-sync. Returns stats dict."""
    logger.info("HxGraph: starting sync…")

    await _sync_case_types(session)
    await _sync_forms(session)
    await _sync_access_groups(session)
    await _sync_access_roles(session)
    await _sync_modules(session)
    await session.commit()

    node_count = (await session.execute(
        select(GraphNodeModel)
    )).all()
    edge_count = (await session.execute(
        select(GraphEdgeModel)
    )).all()

    stats = {
        "nodes": len(node_count),
        "edges": len(edge_count),
        "status": "ok",
    }
    logger.info("HxGraph: sync complete — %s nodes, %s edges", stats["nodes"], stats["edges"])
    return stats
