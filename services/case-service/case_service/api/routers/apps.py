"""App Registry API — P43.

Endpoints:
  POST /apps/package                   snapshot current state → new package
  GET  /apps/packages                  list all packages
  GET  /apps/packages/{id}             package detail + manifest
  PATCH /apps/packages/{id}/status     publish or deprecate
  GET  /apps/packages/{id}/download    download as ZIP
  GET  /apps/packages/{id}/diff/{id2}  diff two packages
  POST /apps/packages/{id}/promote/{env}  record a deployment
  GET  /apps/deployments               full deployment history
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import AppPackageModel, AppDeploymentModel
from case_service.db.session import get_session
from case_service.apps.packager import create_package, build_zip
from case_service.apps.differ import diff_bundles

router = APIRouter(prefix="/apps", tags=["app-registry"])

ENVIRONMENTS = {"dev", "staging", "uat", "prod"}


# ── Schemas ───────────────────────────────────────────────────────────────────

class PackageRequest(BaseModel):
    name: str
    version: str
    description: Optional[str] = None


class StatusUpdate(BaseModel):
    status: str  # published | deprecated


class PromoteRequest(BaseModel):
    notes: Optional[str] = None
    config_overrides: Optional[dict] = None


# ── Package endpoints ─────────────────────────────────────────────────────────

@router.post("/package", status_code=201)
async def package_app(
    body: PackageRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Snapshot the current platform state into a versioned package."""
    try:
        pkg = await create_package(
            session,
            name=body.name,
            version=body.version,
            description=body.description,
            created_by=user.user_id,
        )
    except Exception as e:
        err = str(e).lower()
        if "uq_app_packages" in err or "unique" in err:
            raise HTTPException(409, f"Package '{body.name}' v{body.version} already exists")
        raise HTTPException(500, str(e))

    return _pkg_summary(pkg)


@router.get("/packages")
async def list_packages(
    status: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    stmt = select(AppPackageModel).order_by(desc(AppPackageModel.created_at))
    if status:
        stmt = stmt.where(AppPackageModel.status == status)
    pkgs = (await session.execute(stmt)).scalars().all()
    return {"packages": [_pkg_summary(p) for p in pkgs], "total": len(pkgs)}


@router.get("/packages/{package_id}")
async def get_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    pkg = await _get_or_404(session, package_id)
    result = _pkg_summary(pkg)
    result["manifest"] = pkg.manifest
    result["bundle_meta"] = (pkg.bundle or {}).get("meta", {})
    return result


@router.patch("/packages/{package_id}/status")
async def update_status(
    package_id: uuid.UUID,
    body: StatusUpdate,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    if body.status not in ("published", "deprecated", "draft"):
        raise HTTPException(400, "status must be draft | published | deprecated")
    pkg = await _get_or_404(session, package_id)
    pkg.status = body.status
    await session.commit()
    return {"id": str(pkg.id), "status": pkg.status}


@router.get("/packages/{package_id}/download")
async def download_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Download the package as a ZIP file."""
    pkg = await _get_or_404(session, package_id)
    zip_bytes = build_zip(pkg)
    filename = f"{pkg.name.replace(' ', '_')}_v{pkg.version}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/packages/{package_id}/diff/{other_id}")
async def diff_packages(
    package_id: uuid.UUID,
    other_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Diff two packages. package_id = older, other_id = newer."""
    pkg_a = await _get_or_404(session, package_id)
    pkg_b = await _get_or_404(session, other_id)
    result = diff_bundles(pkg_a.bundle or {}, pkg_b.bundle or {})
    result["package_a"] = {"id": str(pkg_a.id), "name": pkg_a.name, "version": pkg_a.version}
    result["package_b"] = {"id": str(pkg_b.id), "name": pkg_b.name, "version": pkg_b.version}
    return result


# ── Deployment endpoints ──────────────────────────────────────────────────────

@router.post("/packages/{package_id}/promote/{environment}", status_code=201)
async def promote_package(
    package_id: uuid.UUID,
    environment: str = Path(...),
    body: PromoteRequest = PromoteRequest(),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Record a deployment of a package to an environment."""
    if environment not in ENVIRONMENTS:
        raise HTTPException(400, f"environment must be one of: {', '.join(sorted(ENVIRONMENTS))}")
    pkg = await _get_or_404(session, package_id)

    deployment = AppDeploymentModel(
        package_id=pkg.id,
        environment=environment,
        status="deployed",
        deployed_by=user.user_id,
        notes=body.notes,
        config_overrides=body.config_overrides or {},
    )
    session.add(deployment)
    await session.commit()
    await session.refresh(deployment)

    return {
        "id":          str(deployment.id),
        "package_id":  str(pkg.id),
        "package":     f"{pkg.name} v{pkg.version}",
        "environment": environment,
        "status":      deployment.status,
        "deployed_by": deployment.deployed_by,
        "deployed_at": deployment.deployed_at.isoformat(),
    }


@router.get("/deployments")
async def list_deployments(
    environment: Optional[str] = Query(None),
    package_id:  Optional[uuid.UUID] = Query(None),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    stmt = select(AppDeploymentModel).order_by(desc(AppDeploymentModel.deployed_at))
    if environment:
        stmt = stmt.where(AppDeploymentModel.environment == environment)
    if package_id:
        stmt = stmt.where(AppDeploymentModel.package_id == package_id)

    rows = (await session.execute(stmt)).scalars().all()
    results = []
    for d in rows:
        pkg = await session.get(AppPackageModel, d.package_id)
        results.append({
            "id":          str(d.id),
            "package_id":  str(d.package_id),
            "package":     f"{pkg.name} v{pkg.version}" if pkg else "unknown",
            "environment": d.environment,
            "status":      d.status,
            "deployed_by": d.deployed_by,
            "deployed_at": d.deployed_at.isoformat(),
            "notes":       d.notes,
        })
    return {"deployments": results, "total": len(results)}


# ── Utils ─────────────────────────────────────────────────────────────────────

async def _get_or_404(session: AsyncSession, package_id: uuid.UUID) -> AppPackageModel:
    pkg = await session.get(AppPackageModel, package_id)
    if not pkg:
        raise HTTPException(404, f"Package {package_id} not found")
    return pkg


def _pkg_summary(pkg: AppPackageModel) -> dict:
    return {
        "id":          str(pkg.id),
        "name":        pkg.name,
        "version":     pkg.version,
        "description": pkg.description,
        "status":      pkg.status,
        "created_by":  pkg.created_by,
        "created_at":  pkg.created_at.isoformat() if pkg.created_at else None,
    }


# ── Per-case-type export ──────────────────────────────────────────────────────

@router.post("/package/case-type/{case_type_id}", status_code=201)
async def package_case_type(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Bundle one case type + all linked forms into a portable package."""
    from case_service.db.models import CaseTypeModel, FormDefinitionModel
    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        raise HTTPException(404, "Case type not found")

    defn = ct.definition_json or {}
    # Collect form_ids referenced in steps
    form_ids: set[str] = set()
    for stage in defn.get("stages", []):
        for step in stage.get("steps", []):
            if fid := step.get("form_id"):
                form_ids.add(str(fid))

    # Fetch linked forms
    forms = []
    for fid in form_ids:
        try:
            frow = await session.get(FormDefinitionModel, uuid.UUID(fid))
            if frow:
                forms.append({
                    "id": str(frow.id), "name": frow.name, "version": frow.version,
                    "definition_json": frow.definition_json,
                })
        except Exception:
            pass

    # Collect SLA policy IDs from definition
    sla_ids: set[str] = set()
    for stage in defn.get("stages", []):
        for step in stage.get("steps", []):
            if sid := step.get("sla_policy_id"):
                sla_ids.add(str(sid))
        if sid := stage.get("sla_policy_id"):
            sla_ids.add(str(sid))

    # SLA configuration is embedded in definition_json — no separate table to query
    slas = []

    bundle = {
        "schema": "helix-case-bundle/1.0",
        "case_type": {
            "id": str(ct.id), "name": ct.name, "version": ct.version,
            "description": ct.description, "definition_json": defn,
            "default_priority": ct.default_priority, "tags": ct.tags or [],
            "icon": ct.icon, "color": ct.color, "portal_enabled": ct.portal_enabled,
        },
        "forms": forms,
        "sla_policies": slas,
        "exported_by": user.username or user.email or user.user_id,
        "exported_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    return bundle


# ── Import bundle ─────────────────────────────────────────────────────────────

class BundleImportRequest(BaseModel):
    bundle: dict
    version_override: Optional[str] = None  # force a specific version on import


@router.post("/import")
async def import_bundle(
    body: BundleImportRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Import a case-bundle into the current environment.

    Creates or updates the case type, forms and SLA policies in this environment.
    If the name+version already exists, bumps the version by appending -imported.
    """
    from case_service.db.models import CaseTypeModel, FormDefinitionModel
    bundle = body.bundle
    results: dict = {"case_type": None, "forms": [], "sla_policies": [], "warnings": []}

    # ── Case type ──────────────────────────────────────────────────────────────
    ct_data = bundle.get("case_type")
    if ct_data:
        import_version = body.version_override or ct_data.get("version", "1.0.0")
        # Check if this name+version already exists
        from sqlalchemy import select as sa_select
        existing = (await session.execute(
            sa_select(CaseTypeModel).where(
                CaseTypeModel.name == ct_data["name"],
                CaseTypeModel.version == import_version,
            )
        )).scalar_one_or_none()

        if existing:
            # Overwrite definition
            existing.definition_json  = ct_data.get("definition_json", existing.definition_json)
            existing.description      = ct_data.get("description", existing.description)
            existing.default_priority = ct_data.get("default_priority", existing.default_priority)
            existing.tags             = ct_data.get("tags", existing.tags)
            existing.portal_enabled   = ct_data.get("portal_enabled", existing.portal_enabled)
            results["case_type"] = {"id": str(existing.id), "action": "updated", "version": import_version}
        else:
            new_ct = CaseTypeModel(
                name=ct_data["name"],
                version=import_version,
                description=ct_data.get("description", ""),
                definition_json=ct_data.get("definition_json", {}),
                default_priority=ct_data.get("default_priority", "medium"),
                tags=ct_data.get("tags", []),
                icon=ct_data.get("icon"),
                color=ct_data.get("color"),
                portal_enabled=ct_data.get("portal_enabled", False),
            )
            session.add(new_ct)
            await session.flush()
            results["case_type"] = {"id": str(new_ct.id), "action": "created", "version": import_version}

    # ── Forms ──────────────────────────────────────────────────────────────────
    for fd in bundle.get("forms", []):
        import_version = body.version_override or fd.get("version", "1.0.0")
        existing_form = (await session.execute(
            sa_select(FormDefinitionModel).where(
                FormDefinitionModel.name == fd["name"],
                FormDefinitionModel.version == import_version,
            )
        )).scalar_one_or_none()

        if existing_form:
            existing_form.definition_json = fd.get("definition_json", existing_form.definition_json)
            results["forms"].append({"name": fd["name"], "action": "updated"})
        else:
            new_form = FormDefinitionModel(
                name=fd["name"], version=import_version,
                definition_json=fd.get("definition_json", {}),
            )
            session.add(new_form)
            results["forms"].append({"name": fd["name"], "action": "created"})

    await session.commit()
    return results


# ── Version bump ──────────────────────────────────────────────────────────────

class VersionBumpRequest(BaseModel):
    bump_type: str   # patch | minor | major
    changelog: str = ""


def _bump_version(version: str, bump_type: str) -> str:
    parts = version.split(".")
    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2].split("-")[0])
    except (IndexError, ValueError):
        major, minor, patch = 1, 0, 0

    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    else:  # patch
        return f"{major}.{minor}.{patch + 1}"


@router.post("/case-types/{case_type_id}/bump-version", status_code=201)
async def bump_case_type_version(
    case_type_id: uuid.UUID,
    body: VersionBumpRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a new version of a case type (patch | minor | major bump).

    The original version is left intact. A new row is inserted with the bumped version.
    Returns the new case type's ID and version.
    """
    from case_service.db.models import CaseTypeModel
    original = await session.get(CaseTypeModel, case_type_id)
    if not original:
        raise HTTPException(404, "Case type not found")

    new_version = _bump_version(original.version, body.bump_type)

    # Check new version doesn't already exist
    from sqlalchemy import select as sa_select
    conflict = (await session.execute(
        sa_select(CaseTypeModel).where(
            CaseTypeModel.name == original.name,
            CaseTypeModel.version == new_version,
        )
    )).scalar_one_or_none()
    if conflict:
        raise HTTPException(409, f"Version {new_version} already exists for '{original.name}'")

    new_ct = CaseTypeModel(
        name=original.name,
        version=new_version,
        lifecycle_process_id=original.lifecycle_process_id,
        data_model_id=original.data_model_id,
        security_profile_id=original.security_profile_id,
        default_priority=original.default_priority,
        definition_json=dict(original.definition_json or {}),
        icon=original.icon,
        color=original.color,
        description=(f"{original.description or ''}\n\n**v{new_version}:** {body.changelog}".strip()
                     if body.changelog else original.description or ""),
        tags=list(original.tags or []),
        portal_enabled=original.portal_enabled,
    )
    session.add(new_ct)
    await session.commit()

    return {
        "id":           str(new_ct.id),
        "name":         new_ct.name,
        "version":      new_version,
        "previous_version": original.version,
        "bump_type":    body.bump_type,
        "changelog":    body.changelog,
    }


@router.get("/case-types/{case_type_name}/versions")
async def list_case_type_versions(
    case_type_name: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List all versions of a case type by name, newest first."""
    from case_service.db.models import CaseTypeModel
    from sqlalchemy import select as sa_select
    rows = (await session.execute(
        sa_select(CaseTypeModel).where(CaseTypeModel.name == case_type_name)
    )).scalars().all()
    if not rows:
        raise HTTPException(404, f"No case type named '{case_type_name}'")

    def _semver_key(v: str):
        try:
            parts = v.split(".")
            return tuple(int(p.split("-")[0]) for p in parts)
        except Exception:
            return (0, 0, 0)

    sorted_rows = sorted(rows, key=lambda r: _semver_key(r.version), reverse=True)
    return [{"id": str(r.id), "name": r.name, "version": r.version, "created_at": r.created_at.isoformat()} for r in sorted_rows]
