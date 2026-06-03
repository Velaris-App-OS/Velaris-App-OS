"""P43 — App Packager.

Captures the current platform state into a versioned bundle:
  case_types, forms, rules, portals, access_groups, work_queues,
  escalation_trees, business_calendars + applied migration history.

The bundle is stored as JSONB in app_packages.bundle and can also
be downloaded as a ZIP file.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    AppPackageModel,
    AccessGroupModel,
    BusinessCalendarModel,
    CaseTypeModel,
    EscalationTreeModel,
    FormDefinitionModel,
    PortalModel,
    RuleDefinitionModel,
    WorkQueueModel,
)

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checksum(obj: object) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ── Section collectors ────────────────────────────────────────────────────────

async def _collect_case_types(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.is_deleted.is_(False))
    )).scalars().all()
    return [
        {
            "id": str(r.id), "name": r.name, "version": r.version,
            "description": r.description or "",
            "definition_json": r.definition_json or {},
            "default_priority": r.default_priority,
            "portal_enabled": r.portal_enabled,
            "tags": r.tags or [],
            "icon": r.icon, "color": r.color,
        }
        for r in rows
    ]


async def _collect_forms(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(FormDefinitionModel))).scalars().all()
    return [
        {
            "id": str(r.id), "name": r.name, "version": r.version,
            "definition_json": r.definition_json or {},
        }
        for r in rows
    ]


async def _collect_rules(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(RuleDefinitionModel))).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": getattr(r, "name", ""),
            "rule_type": getattr(r, "rule_type", ""),
            "definition": getattr(r, "definition", {}),
        }
        for r in rows
    ]


async def _collect_portals(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(PortalModel))).scalars().all()
    return [
        {
            "id":          str(r.id),
            "name":        r.name,
            "portal_type": r.portal_type,
            "homepage":    r.homepage,
            "modules":     r.modules or [],
            "theme":       r.theme or {},
            "tenant_id":   r.tenant_id,
            "is_active":   r.is_active,
        }
        for r in rows
    ]


async def _collect_access_groups(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(AccessGroupModel))).scalars().all()
    return [
        {
            "id": str(r.id), "name": r.name,
            "description": r.description or "",
            "tenant_id": r.tenant_id,
            "role_ids": r.role_ids or [],
            "allowed_case_type_ids": r.allowed_case_type_ids or [],
            "is_default": r.is_default,
        }
        for r in rows
    ]


async def _collect_work_queues(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(WorkQueueModel))).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": getattr(r, "name", ""),
            "description": getattr(r, "description", ""),
        }
        for r in rows
    ]


async def _collect_escalation_trees(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(EscalationTreeModel))).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": getattr(r, "name", ""),
            "definition": getattr(r, "definition_json", {}),
        }
        for r in rows
    ]


async def _collect_business_calendars(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(BusinessCalendarModel))).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": getattr(r, "name", ""),
            "definition": getattr(r, "definition", {}),
        }
        for r in rows
    ]


async def _collect_migrations(session: AsyncSession) -> list[dict]:
    """Return list of applied migrations.

    Each query runs inside its own SAVEPOINT so a failure (missing table,
    permission denied) does NOT abort the outer transaction.
    """
    for query, extractor in [
        (
            text("SELECT version_num FROM alembic_version LIMIT 50"),
            lambda row: {"version": row[0]},
        ),
        (
            text("SELECT name, applied_at FROM schema_migrations ORDER BY applied_at"),
            lambda row: {"name": row[0], "applied_at": str(row[1])},
        ),
    ]:
        try:
            async with session.begin_nested():   # SAVEPOINT — isolated from outer tx
                result = await session.execute(query)
                rows = result.all()
            return [extractor(r) for r in rows]
        except Exception:
            pass  # SAVEPOINT automatically rolled back; outer tx unaffected

    return []


# ── Main packager ─────────────────────────────────────────────────────────────

async def capture_bundle(session: AsyncSession) -> dict:
    """Capture the current platform state as a bundle dict."""
    case_types        = await _collect_case_types(session)
    forms             = await _collect_forms(session)
    rules             = await _collect_rules(session)
    portals           = await _collect_portals(session)
    access_groups     = await _collect_access_groups(session)
    work_queues       = await _collect_work_queues(session)
    escalation_trees  = await _collect_escalation_trees(session)
    business_calendars= await _collect_business_calendars(session)
    migrations        = await _collect_migrations(session)

    bundle = {
        "meta": {
            "packaged_at":   _utcnow(),
            "helix_version": "1.0",
        },
        "case_types":         case_types,
        "forms":              forms,
        "rules":              rules,
        "portals":            portals,
        "access_groups":      access_groups,
        "work_queues":        work_queues,
        "escalation_trees":   escalation_trees,
        "business_calendars": business_calendars,
        "migrations":         migrations,
    }

    manifest = {
        "case_types":         len(case_types),
        "forms":              len(forms),
        "rules":              len(rules),
        "portals":            len(portals),
        "access_groups":      len(access_groups),
        "work_queues":        len(work_queues),
        "escalation_trees":   len(escalation_trees),
        "business_calendars": len(business_calendars),
        "migrations":         len(migrations),
        "bundle_checksum":    _checksum(bundle),
    }
    bundle["meta"]["manifest"] = manifest
    return bundle, manifest


async def create_package(
    session: AsyncSession,
    name: str,
    version: str,
    description: str | None,
    created_by: str | None,
) -> AppPackageModel:
    """Snapshot current state and save as a new app_package row."""
    bundle, manifest = await capture_bundle(session)
    pkg = AppPackageModel(
        name=name,
        version=version,
        description=description or "",
        bundle=bundle,
        manifest=manifest,
        created_by=created_by,
        status="draft",
    )
    session.add(pkg)
    await session.commit()
    await session.refresh(pkg)
    logger.info("App packaged: %s v%s (%s)", name, version, pkg.id)
    return pkg


def build_zip(pkg: AppPackageModel) -> bytes:
    """Build a downloadable ZIP from an app_package row."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Top-level manifest
        zf.writestr("helix-app.json", json.dumps({
            "name":        pkg.name,
            "version":     pkg.version,
            "description": pkg.description,
            "packaged_at": pkg.bundle.get("meta", {}).get("packaged_at", ""),
            "manifest":    pkg.manifest,
        }, indent=2))

        bundle = pkg.bundle or {}

        # One file per section
        for section in ["case_types", "forms", "rules", "portals",
                         "access_groups", "work_queues", "escalation_trees",
                         "business_calendars"]:
            items = bundle.get(section, [])
            if items:
                zf.writestr(f"{section}.json", json.dumps(items, indent=2, default=str))

        # Migrations list
        migrations = bundle.get("migrations", [])
        if migrations:
            zf.writestr("migrations.json", json.dumps(migrations, indent=2, default=str))

        # README
        manifest = pkg.manifest or {}
        readme = "\n".join([
            f"# {pkg.name} v{pkg.version}",
            f"",
            f"Packaged from HELIX BPM Platform.",
            f"",
            f"## Contents",
            *[f"- {k}: {v}" for k, v in manifest.items() if isinstance(v, int)],
            f"",
            f"## How to Apply",
            f"1. Import via POST /api/v1/apps/import (coming in P44)",
            f"2. Or manually apply each JSON file via the respective API",
        ])
        zf.writestr("README.md", readme)

    return buf.getvalue()
