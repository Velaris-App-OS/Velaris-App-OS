"""P60 HxBranch v2 — Artifact Version Control & Live Environment Sync.

Branches let developers pull Helix artifacts (case types, forms,
integrations) or full app packages from any registered environment
(staging, UAT, etc.) back to dev for review and merge into main.

v2 adds: ownership enforcement, SOD (reviewer ≠ owner), auto-merge on
approval, recall, revert-to-base, and Work Center reviewer assignment.

Dev is always main — the source of truth for all promotions.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    ArtifactBranchModel,
    BranchAuditEventModel,
    BranchReviewModel,
    ComponentCommitModel,
    EnvironmentRegistryModel,
    HelixUserModel,
    HxWorkStoryModel,
)
from case_service.db.session import get_session
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.hxbridge.encryption import encrypt_credentials, decrypt_credentials

router = APIRouter(prefix="/branches", tags=["hxbranch"])

VALID_ARTIFACT_TYPES = {"case_type", "form", "integration", "rule", "escalation"}
VALID_STATUSES = {"open", "pending_review", "approved", "merged", "rejected", "closed"}
VALID_DECISIONS = {"approved", "rejected", "changes_requested"}


# ── Pydantic schemas ──────────────────────────────────────────────

class CreateBranchRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    branch_type: str = "artifact"
    artifact_type: Optional[str] = None
    artifact_id: Optional[str] = None

class PullBranchRequest(BaseModel):
    env_id: uuid.UUID
    branch_name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    branch_type: str = "artifact"
    artifact_type: Optional[str] = None
    artifact_id: Optional[str] = None  # remote artifact UUID

class UpdateBranchRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None

class SubmitForReviewRequest(BaseModel):
    assigned_reviewer_id: str = Field(..., min_length=1, description="user_id of the reviewer")

class PostReviewRequest(BaseModel):
    decision: str
    comments: Optional[str] = None

class UpdateBranchContentRequest(BaseModel):
    content_snapshot: dict

class SetEnvTokenRequest(BaseModel):
    api_token: str = Field(..., min_length=1)


# ── Serialisers ───────────────────────────────────────────────────

def _branch(b: ArtifactBranchModel) -> dict:
    return {
        "id": str(b.id),
        "name": b.name,
        "description": b.description,
        "branch_type": b.branch_type,
        "artifact_type": b.artifact_type,
        "artifact_id": b.artifact_id,
        "app_package_id": str(b.app_package_id) if b.app_package_id else None,
        "source_env_id": str(b.source_env_id) if b.source_env_id else None,
        "source_env_name": b.source_env_name,
        "status": b.status,
        "conflict_detected": b.conflict_detected,
        # v2 ownership fields
        "owner_id": b.owner_id,
        "assigned_reviewer_id": b.assigned_reviewer_id,
        "access_group_id": str(b.access_group_id) if b.access_group_id else None,
        "created_by": b.created_by,
        "reviewed_by": b.reviewed_by,
        "merged_by": b.merged_by,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
        "merged_at": b.merged_at.isoformat() if b.merged_at else None,
        "content_snapshot": b.content_snapshot,
    }

def _review(r: BranchReviewModel) -> dict:
    return {
        "id": str(r.id),
        "branch_id": str(r.branch_id),
        "reviewer_id": r.reviewer_id,
        "decision": r.decision,
        "comments": r.comments,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }

def _audit_event(e: BranchAuditEventModel) -> dict:
    return {
        "id": str(e.id),
        "branch_id": str(e.branch_id),
        "event_type": e.event_type,
        "actor_id": e.actor_id,
        "actor_name": e.actor_name,
        "metadata": e.event_metadata,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


# ── Helpers ───────────────────────────────────────────────────────

async def _resolve_username(session: AsyncSession, user_id: str) -> str:
    """Return the username for a user_id UUID, falling back to the id itself."""
    try:
        u = await session.get(HelixUserModel, uuid.UUID(user_id))
        if u and u.username:
            return u.username
    except Exception:
        pass
    return user_id


async def _write_merge_commit(
    session: AsyncSession,
    branch: ArtifactBranchModel,
    current_main: dict,
    actor_username: str,
) -> None:
    """Write a ComponentCommitModel so the merge appears in the artifact's History tab."""
    if not branch.artifact_type or not branch.artifact_id:
        return
    try:
        total = (branch.merge_diff or {}).get("total_changes", 0)
        commit = ComponentCommitModel(
            component_type=branch.artifact_type,
            component_id=branch.artifact_id,
            component_name=branch.name,
            commit_message=f"Merged branch '{branch.name}' — {total} field(s) changed",
            committed_by=actor_username,
            diff_snapshot={"before": current_main, "after": branch.content_snapshot},
        )
        session.add(commit)
    except Exception:
        pass


async def _log_event(
    session: AsyncSession,
    branch_id: uuid.UUID,
    event_type: str,
    user: Optional[Any] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Append an immutable audit event for a branch (non-blocking — errors are swallowed)."""
    try:
        ev = BranchAuditEventModel(
            branch_id=branch_id,
            event_type=event_type,
            actor_id=getattr(user, "user_id", None),
            actor_name=getattr(user, "username", None),
            event_metadata=metadata or {},
        )
        session.add(ev)
    except Exception:
        pass  # audit must never block the main operation


async def _get_branch_or_404(session: AsyncSession, branch_id: uuid.UUID) -> ArtifactBranchModel:
    b = await session.get(ArtifactBranchModel, branch_id)
    if not b:
        raise HTTPException(404, "Branch not found")
    return b

async def _get_env_or_404(session: AsyncSession, env_id: uuid.UUID) -> EnvironmentRegistryModel:
    e = await session.get(EnvironmentRegistryModel, env_id)
    if not e:
        raise HTTPException(404, "Environment not found")
    return e

def _decrypt_token(env: EnvironmentRegistryModel) -> str:
    if not env.api_token_enc:
        raise HTTPException(400, f"No API token configured for environment '{env.label}'. Set it in HxBranch → Connections.")
    return decrypt_credentials(env.api_token_enc)["token"]

def _compute_diff(base: dict, current: dict) -> dict:
    """Simple diff between two artifact snapshots."""
    changed_fields: list[dict] = []
    all_keys = set(base.keys()) | set(current.keys())
    for key in sorted(all_keys):
        base_val = base.get(key)
        curr_val = current.get(key)
        if base_val != curr_val:
            changed_fields.append({
                "field": key,
                "base": base_val,
                "branch": curr_val,
            })
    return {
        "changed_fields": changed_fields,
        "added_keys":   [k for k in current if k not in base],
        "removed_keys": [k for k in base if k not in current],
        "total_changes": len(changed_fields),
    }

def _detect_conflict(base_snapshot: dict, current_main: dict) -> bool:
    """True if main has changed since the branch was created."""
    import hashlib, json
    def chk(d: dict) -> str:
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()
    return chk(base_snapshot) != chk(current_main)

async def _fetch_remote(env: EnvironmentRegistryModel, path: str) -> Any:
    """Make an authenticated GET to a remote Helix environment."""
    if not env.url:
        raise HTTPException(400, f"Environment '{env.label}' has no URL configured.")
    token = _decrypt_token(env)
    url = env.url.rstrip("/") + path
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        raise HTTPException(400, f"API token for '{env.label}' is invalid or expired.")
    if not resp.is_success:
        raise HTTPException(502, f"Remote environment '{env.label}' returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()

async def _get_local_artifact(session: AsyncSession, artifact_type: str, artifact_id: str) -> dict:
    """Fetch the current main version of an artifact from the local dev environment."""
    if artifact_type == "case_type":
        from case_service.db.models import CaseTypeModel
        ct = await session.get(CaseTypeModel, uuid.UUID(artifact_id))
        if not ct:
            return {}
        return {
            "id": str(ct.id), "name": ct.name, "version": ct.version,
            "definition_json": ct.definition_json,
            "default_priority": ct.default_priority,
            "description": ct.description,
        }
    if artifact_type == "form":
        from case_service.db.models import FormDefinitionModel
        f = await session.get(FormDefinitionModel, uuid.UUID(artifact_id))
        if not f:
            return {}
        return {"id": str(f.id), "name": f.name, "version": f.version, "definition_json": f.definition_json}
    if artifact_type == "rule":
        from case_service.db.models import RuleDefinitionModel
        r = await session.get(RuleDefinitionModel, uuid.UUID(artifact_id))
        if not r:
            return {}
        return {"id": str(r.id), "name": r.name, "version": r.version, "rule_type": r.rule_type, "definition_json": r.definition_json, "enabled": r.enabled}
    if artifact_type in ("integration", "connector"):
        from case_service.db.models import ConnectorRegistryModel
        c = await session.get(ConnectorRegistryModel, uuid.UUID(artifact_id))
        if not c:
            return {}
        # Never include credentials — they contain encrypted secrets
        return {"id": str(c.id), "name": c.name, "connector_type": c.connector_type, "description": c.description, "config": c.config, "enabled": c.enabled}
    if artifact_type == "escalation":
        from case_service.db.models import EscalationTreeModel
        e = await session.get(EscalationTreeModel, uuid.UUID(artifact_id))
        if not e:
            return {}
        return {"id": str(e.id), "name": e.name, "description": e.description, "scope": e.scope, "tree_json": e.tree_json, "is_active": e.is_active}
    return {}

async def _apply_artifact_to_main(session: AsyncSession, artifact_type: str, artifact_id: str, content: dict) -> None:
    """Write branch content into the local dev artifact (the merge operation)."""
    now = datetime.now(timezone.utc)
    if artifact_type == "case_type":
        from case_service.db.models import CaseTypeModel
        ct = await session.get(CaseTypeModel, uuid.UUID(artifact_id))
        if not ct:
            raise HTTPException(404, f"Case type {artifact_id} not found in dev — cannot merge.")
        if "definition_json" in content:
            ct.definition_json = content["definition_json"]
        if "description" in content and content["description"]:
            ct.description = content["description"]
        ct.updated_at = now
        return
    if artifact_type == "form":
        from case_service.db.models import FormDefinitionModel
        f = await session.get(FormDefinitionModel, uuid.UUID(artifact_id))
        if not f:
            raise HTTPException(404, f"Form {artifact_id} not found in dev — cannot merge.")
        if "definition_json" in content:
            f.definition_json = content["definition_json"]
        f.updated_at = now
        return
    if artifact_type == "rule":
        from case_service.db.models import RuleDefinitionModel
        r = await session.get(RuleDefinitionModel, uuid.UUID(artifact_id))
        if not r:
            raise HTTPException(404, f"Rule {artifact_id} not found in dev — cannot merge.")
        if "definition_json" in content:
            r.definition_json = content["definition_json"]
        if "enabled" in content:
            r.enabled = content["enabled"]
        r.updated_at = now
        return
    if artifact_type in ("integration", "connector"):
        from case_service.db.models import ConnectorRegistryModel
        c = await session.get(ConnectorRegistryModel, uuid.UUID(artifact_id))
        if not c:
            raise HTTPException(404, f"Connector {artifact_id} not found in dev — cannot merge.")
        # NEVER write credentials — that would wipe encrypted live secrets
        if "config" in content:
            c.config = content["config"]
        if "name" in content:
            c.name = content["name"]
        if "enabled" in content:
            c.enabled = content["enabled"]
        c.updated_at = now
        return
    if artifact_type == "escalation":
        from case_service.db.models import EscalationTreeModel
        e = await session.get(EscalationTreeModel, uuid.UUID(artifact_id))
        if not e:
            raise HTTPException(404, f"Escalation tree {artifact_id} not found in dev — cannot merge.")
        if "tree_json" in content:
            e.tree_json = content["tree_json"]
        if "is_active" in content:
            e.is_active = content["is_active"]
        e.updated_at = now
        return
    raise HTTPException(400, f"Merge not yet supported for artifact_type='{artifact_type}'.")


async def _queue_rule_regen(
    session: AsyncSession,
    background_tasks: BackgroundTasks,
    artifact_type: Optional[str],
    artifact_id: Optional[str],
) -> None:
    """Parity with the manual rules PATCH: a merged rule change stales AI scenarios."""
    if artifact_type != "rule" or not artifact_id:
        return
    try:
        from case_service.db.models import RuleDefinitionModel
        rule = await session.get(RuleDefinitionModel, uuid.UUID(artifact_id))
        from case_service.testsuite import regen
        background_tasks.add_task(
            regen.bg_scenario_source_changed,
            rule.scope_target_id if rule else None,
        )
    except Exception:
        pass  # regen queueing must never block a merge


# ── Branches CRUD ─────────────────────────────────────────────────

@router.get("")
async def list_branches(
    status:               Optional[str] = None,
    branch_type:          Optional[str] = None,
    artifact_type:        Optional[str] = None,
    artifact_id:          Optional[str] = None,
    owner_id:             Optional[str] = None,
    assigned_reviewer_id: Optional[str] = None,
    q:                    Optional[str] = None,
    session:              AsyncSession = Depends(get_session),
    _:                    AuthenticatedUser = Depends(get_current_user),
):
    stmt = select(ArtifactBranchModel).order_by(desc(ArtifactBranchModel.created_at))
    if status:               stmt = stmt.where(ArtifactBranchModel.status == status)
    if branch_type:          stmt = stmt.where(ArtifactBranchModel.branch_type == branch_type)
    if artifact_type:        stmt = stmt.where(ArtifactBranchModel.artifact_type == artifact_type)
    if artifact_id:          stmt = stmt.where(ArtifactBranchModel.artifact_id == artifact_id)
    if owner_id:             stmt = stmt.where(ArtifactBranchModel.owner_id == owner_id)
    if assigned_reviewer_id: stmt = stmt.where(ArtifactBranchModel.assigned_reviewer_id == assigned_reviewer_id)
    if q:                    stmt = stmt.where(ArtifactBranchModel.name.ilike(f"%{q}%"))
    rows = (await session.execute(stmt)).scalars().all()
    return {"branches": [_branch(b) for b in rows], "total": len(rows)}


@router.post("", status_code=201)
async def create_branch(
    body:    CreateBranchRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    """Create a branch manually from an artifact already in dev (for local branching)."""
    if body.branch_type == "artifact":
        if not body.artifact_type or body.artifact_type not in VALID_ARTIFACT_TYPES:
            raise HTTPException(400, f"artifact_type must be one of: {VALID_ARTIFACT_TYPES}")
        if not body.artifact_id:
            raise HTTPException(400, "artifact_id is required for artifact-level branches")

    local_content = {}
    if body.artifact_type and body.artifact_id:
        local_content = await _get_local_artifact(session, body.artifact_type, body.artifact_id)

    ag = user.active_access_group
    ag_id = uuid.UUID(ag.id) if ag and ag.id else None

    b = ArtifactBranchModel(
        name=body.name, description=body.description,
        branch_type=body.branch_type, artifact_type=body.artifact_type,
        artifact_id=body.artifact_id,
        source_env_name="dev (local)", status="open",
        content_snapshot=local_content, base_snapshot=local_content,
        created_by=user.username,
        owner_id=user.user_id,
        access_group_id=ag_id,
    )
    session.add(b)
    await session.flush()
    await _log_event(session, b.id, "branch_created", user, {
        "branch_name": b.name,
        "artifact_type": b.artifact_type,
        "artifact_id": b.artifact_id,
        "description": b.description,
    })
    await session.commit()
    await session.refresh(b)
    return _branch(b)


@router.get("/{branch_id}")
async def get_branch(
    branch_id: uuid.UUID,
    session:   AsyncSession = Depends(get_session),
    _:         AuthenticatedUser = Depends(get_current_user),
):
    b = await _get_branch_or_404(session, branch_id)
    data = _branch(b)

    # Resolve reviewer UUID → human-readable username
    if b.assigned_reviewer_id:
        data["assigned_reviewer_name"] = await _resolve_username(session, b.assigned_reviewer_id)

    # Compute live diff against current main
    if b.artifact_type and b.artifact_id:
        current_main = await _get_local_artifact(session, b.artifact_type, b.artifact_id)
        data["diff_vs_main"]  = _compute_diff(b.content_snapshot, current_main)
        data["diff_from_base"] = _compute_diff(b.base_snapshot, b.content_snapshot)
        data["conflict_detected"] = _detect_conflict(b.base_snapshot, current_main)
    return data


@router.patch("/{branch_id}")
async def update_branch(
    branch_id: uuid.UUID,
    body:      UpdateBranchRequest,
    session:   AsyncSession = Depends(get_session),
    _:         AuthenticatedUser = Depends(get_current_user),
):
    b = await _get_branch_or_404(session, branch_id)
    if b.status in ("merged", "closed"):
        raise HTTPException(400, f"Cannot edit a branch with status '{b.status}'.")
    if body.name is not None:   b.name = body.name
    if body.description is not None: b.description = body.description
    if body.status is not None:
        if body.status not in VALID_STATUSES:
            raise HTTPException(400, f"Invalid status. Must be one of: {VALID_STATUSES}")
        b.status = body.status
    b.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(b)
    return _branch(b)


@router.patch("/{branch_id}/content")
async def update_branch_content(
    branch_id: uuid.UUID,
    body:      UpdateBranchContentRequest,
    session:   AsyncSession = Depends(get_session),
    user:      AuthenticatedUser = Depends(get_current_user),
):
    """Replace the content snapshot of a branch (edit-in-branch flow).

    Only the branch owner may update content. Branch is read-only while
    under review (status = pending_review).
    """
    b = await _get_branch_or_404(session, branch_id)

    # Admins bypass ownership check; regular users must be the owner
    if not user.is_admin and b.owner_id and b.owner_id != user.user_id:
        raise HTTPException(403, "Only the branch owner may edit its content.")

    if b.status == "pending_review":
        raise HTTPException(423, "Branch is locked for review. Recall the branch first to make changes.")
    if b.status in ("merged", "closed", "approved"):
        raise HTTPException(400, f"Cannot edit a branch with status '{b.status}'.")

    b.content_snapshot = body.content_snapshot
    b.updated_at = datetime.now(timezone.utc)
    await _log_event(session, b.id, "content_saved", user)
    await session.commit()
    await session.refresh(b)
    return _branch(b)


@router.delete("/{branch_id}", status_code=204)
async def delete_branch(
    branch_id: uuid.UUID,
    session:   AsyncSession = Depends(get_session),
    user:      AuthenticatedUser = Depends(get_current_user),
):
    b = await _get_branch_or_404(session, branch_id)
    await _log_event(session, b.id, "branch_deleted", user, {"branch_name": b.name, "status": b.status})
    await session.delete(b)
    await session.commit()


# ── Pull from remote environment ──────────────────────────────────

@router.get("/remote/{env_id}/available")
async def list_remote_available(
    env_id:        uuid.UUID,
    artifact_type: str = "case_type",
    session:       AsyncSession = Depends(get_session),
    _:             AuthenticatedUser = Depends(get_current_user),
):
    """List artifacts available to pull from a registered remote environment."""
    env = await _get_env_or_404(session, env_id)
    path_map = {
        "case_type":   "/api/v1/case-types",
        "form":        "/api/v1/forms",
        "integration": "/api/v1/hxbridge/connectors",
        "rule":        "/api/v1/rules",
        "escalation":  "/api/v1/escalation-trees",
        "app":         "/api/v1/apps/packages",
    }
    path = path_map.get(artifact_type, "/api/v1/case-types")
    data = await _fetch_remote(env, path)
    return {
        "env_id": str(env_id),
        "env_name": env.label,
        "artifact_type": artifact_type,
        "items": data,
    }


@router.post("/pull", status_code=201)
async def pull_branch_from_env(
    body:    PullBranchRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    """Pull an artifact or app package from a remote environment and create a local branch."""
    env = await _get_env_or_404(session, body.env_id)

    # Fetch the artifact from the remote environment
    content_snapshot: dict = {}

    if body.branch_type == "app":
        # Pull full app package
        data = await _fetch_remote(env, "/api/v1/apps/packages")
        packages = data if isinstance(data, list) else data.get("packages", [])
        if not packages:
            raise HTTPException(404, "No app packages found in remote environment.")
        # Take the latest published package
        content_snapshot = packages[0] if packages else {}

    elif body.branch_type == "artifact":
        if not body.artifact_type or body.artifact_type not in VALID_ARTIFACT_TYPES:
            raise HTTPException(400, f"artifact_type must be one of: {VALID_ARTIFACT_TYPES}")
        if not body.artifact_id:
            raise HTTPException(400, "artifact_id is required when pulling an artifact-level branch")

        path_map = {
            "case_type":   f"/api/v1/case-types/{body.artifact_id}",
            "form":        f"/api/v1/forms/{body.artifact_id}",
            "integration": f"/api/v1/hxbridge/connectors/{body.artifact_id}",
            "rule":        f"/api/v1/rules/{body.artifact_id}",
            "escalation":  f"/api/v1/escalation-trees/{body.artifact_id}",
        }
        path = path_map.get(body.artifact_type)
        if not path:
            raise HTTPException(400, f"Pull not supported for artifact_type='{body.artifact_type}'")
        content_snapshot = await _fetch_remote(env, path)
    else:
        raise HTTPException(400, "branch_type must be 'app' or 'artifact'")

    # Snapshot current main (what dev has right now) — used for conflict detection
    base_snapshot: dict = {}
    if body.artifact_type and body.artifact_id:
        base_snapshot = await _get_local_artifact(session, body.artifact_type, body.artifact_id)

    ag = user.active_access_group
    ag_id = uuid.UUID(ag.id) if ag and ag.id else None

    b = ArtifactBranchModel(
        name=body.branch_name,
        description=body.description,
        branch_type=body.branch_type,
        artifact_type=body.artifact_type,
        artifact_id=body.artifact_id,
        source_env_id=env.id,
        source_env_name=env.label,
        status="open",
        content_snapshot=content_snapshot,
        base_snapshot=base_snapshot,
        conflict_detected=False,
        created_by=user.username,
        owner_id=user.user_id,
        access_group_id=ag_id,
    )
    session.add(b)
    await session.flush()
    await _log_event(session, b.id, "branch_created", user, {
        "branch_name": b.name,
        "source_env": env.label,
        "artifact_type": b.artifact_type,
        "artifact_id": b.artifact_id,
        "pulled_from_env": True,
    })
    await session.commit()
    await session.refresh(b)
    result = _branch(b)

    # Check for conflicts now
    if base_snapshot and content_snapshot:
        result["diff_vs_main"]   = _compute_diff(content_snapshot, base_snapshot)
        result["diff_from_base"] = _compute_diff(base_snapshot, content_snapshot)
    return result


# ── Review flow ───────────────────────────────────────────────────

@router.post("/{branch_id}/submit")
async def submit_for_review(
    branch_id: uuid.UUID,
    body:      SubmitForReviewRequest,
    session:   AsyncSession = Depends(get_session),
    user:      AuthenticatedUser = Depends(get_current_user),
):
    """Submit a branch for review, assigning a specific reviewer.

    SOD rule: reviewer ≠ owner. Enforced here — admin bypass is intentional
    for emergency reviews where a team has only one member.
    """
    b = await _get_branch_or_404(session, branch_id)

    if b.status != "open":
        raise HTTPException(400, f"Branch must be 'open' to submit for review (current: '{b.status}').")

    # Ownership check — only the owner (or admin) may submit
    if not user.is_admin and b.owner_id and b.owner_id != user.user_id:
        raise HTTPException(403, "Only the branch owner may submit it for review.")

    # SOD: reviewer must differ from the owner
    owner = b.owner_id or user.user_id
    if body.assigned_reviewer_id == owner:
        raise HTTPException(400, "Separation of duties: the reviewer must be a different person from the branch owner.")

    reviewer_name = await _resolve_username(session, body.assigned_reviewer_id)
    b.status = "pending_review"
    b.assigned_reviewer_id = body.assigned_reviewer_id
    b.updated_at = datetime.now(timezone.utc)
    await _log_event(session, b.id, "submitted_for_review", user, {
        "assigned_reviewer_id": body.assigned_reviewer_id,
        "assigned_reviewer_name": reviewer_name,
    })
    await session.commit()
    return _branch(b)


@router.get("/{branch_id}/reviews")
async def list_reviews(
    branch_id: uuid.UUID,
    session:   AsyncSession = Depends(get_session),
    _:         AuthenticatedUser = Depends(get_current_user),
):
    await _get_branch_or_404(session, branch_id)
    rows = (await session.execute(
        select(BranchReviewModel)
        .where(BranchReviewModel.branch_id == branch_id)
        .order_by(BranchReviewModel.created_at)
    )).scalars().all()
    return {"reviews": [_review(r) for r in rows]}


@router.post("/{branch_id}/reviews", status_code=201)
async def post_review(
    branch_id: uuid.UUID,
    body:      PostReviewRequest,
    background_tasks: BackgroundTasks,
    session:   AsyncSession = Depends(get_session),
    user:      AuthenticatedUser = Depends(get_current_user),
):
    """Post a review decision. Only the assigned reviewer (or admin) may act.

    On 'approved': auto-merge fires immediately — no separate merge step.
    On 'changes_requested': branch reopens for the owner to re-edit.
    On 'rejected': branch closes.
    """
    if body.decision not in VALID_DECISIONS:
        raise HTTPException(400, f"decision must be one of: {VALID_DECISIONS}")

    b = await _get_branch_or_404(session, branch_id)
    if b.status != "pending_review":
        raise HTTPException(400, f"Branch must be 'pending_review' to post a review (current: '{b.status}').")

    # Only the assigned reviewer (or admin) may decide
    if not user.is_admin and b.assigned_reviewer_id and b.assigned_reviewer_id != user.user_id:
        raise HTTPException(403, "Only the assigned reviewer may post a review decision.")

    now = datetime.now(timezone.utc)

    rev = BranchReviewModel(
        branch_id=branch_id,
        reviewer_id=user.user_id,
        decision=body.decision,
        comments=body.comments,
    )
    session.add(rev)

    if body.decision == "approved":
        # Auto-merge: apply artifact directly to main, then mark merged
        current_main: dict = {}
        if b.artifact_type and b.artifact_id:
            current_main = await _get_local_artifact(session, b.artifact_type, b.artifact_id)
            if _detect_conflict(b.base_snapshot, current_main):
                raise HTTPException(
                    409,
                    "Conflict detected: dev main has changed since this branch was created. "
                    "The owner must recall the branch, rebase, and resubmit.",
                )
            await _apply_artifact_to_main(session, b.artifact_type, b.artifact_id, b.content_snapshot)
            await _queue_rule_regen(session, background_tasks, b.artifact_type, b.artifact_id)

        merge_diff = _compute_diff(current_main, b.content_snapshot)
        b.status = "merged"
        b.reviewed_by = user.user_id
        b.merged_by = user.user_id
        b.merged_at = now
        b.merge_diff = merge_diff

        await _write_merge_commit(session, b, current_main, user.username or user.user_id)
        await _log_event(session, b.id, "reviewed", user, {
            "decision": "approved",
            "comments": body.comments,
        })
        await _log_event(session, b.id, "merged", user, {
            "artifact_type": b.artifact_type,
            "artifact_id": b.artifact_id,
            "changes": merge_diff.get("total_changes", 0),
        })

        # Auto-advance any linked story to Done
        linked_story = (await session.execute(
            select(HxWorkStoryModel).where(HxWorkStoryModel.branch_id == branch_id)
        )).scalar_one_or_none()
        if linked_story and linked_story.status != "done":
            linked_story.status = "done"
            linked_story.updated_at = now

    elif body.decision == "rejected":
        b.status = "rejected"
        b.reviewed_by = user.user_id
        await _log_event(session, b.id, "reviewed", user, {
            "decision": "rejected",
            "comments": body.comments,
        })

    else:  # changes_requested
        # Reopen so the owner can re-edit and resubmit
        b.status = "open"
        b.assigned_reviewer_id = None  # owner must re-pick reviewer on next submit
        await _log_event(session, b.id, "reviewed", user, {
            "decision": "changes_requested",
            "comments": body.comments,
        })

    b.updated_at = now
    await session.commit()
    await session.refresh(rev)
    result = _review(rev)
    if body.decision == "approved":
        result["auto_merged"] = True
    return result


# ── Diff ─────────────────────────────────────────────────────────

@router.get("/{branch_id}/diff")
async def get_diff(
    branch_id: uuid.UUID,
    session:   AsyncSession = Depends(get_session),
    _:         AuthenticatedUser = Depends(get_current_user),
):
    """Full diff: branch content vs current dev main, plus conflict analysis."""
    b = await _get_branch_or_404(session, branch_id)

    current_main: dict = {}
    if b.artifact_type and b.artifact_id:
        current_main = await _get_local_artifact(session, b.artifact_type, b.artifact_id)

    diff_vs_main   = _compute_diff(b.content_snapshot, current_main)
    diff_from_base = _compute_diff(b.base_snapshot, b.content_snapshot)
    conflict       = _detect_conflict(b.base_snapshot, current_main)

    return {
        "branch_id":      str(branch_id),
        "branch_name":    b.name,
        "artifact_type":  b.artifact_type,
        "artifact_id":    b.artifact_id,
        "source_env":     b.source_env_name,
        "conflict":       conflict,
        "diff_vs_main":   diff_vs_main,    # what will change in dev if you merge
        "diff_from_base": diff_from_base,  # what changed between when pulled and now in branch
        "current_main_snapshot":  current_main,
        "branch_snapshot":        b.content_snapshot,
    }


# ── Audit trail ──────────────────────────────────────────────────

@router.get("/{branch_id}/audit")
async def get_branch_audit(
    branch_id: uuid.UUID,
    session:   AsyncSession = Depends(get_session),
    _:         AuthenticatedUser = Depends(get_current_user),
):
    """Return the full chronological audit trail for a branch."""
    await _get_branch_or_404(session, branch_id)
    rows = (await session.execute(
        select(BranchAuditEventModel)
        .where(BranchAuditEventModel.branch_id == branch_id)
        .order_by(BranchAuditEventModel.created_at)
    )).scalars().all()
    return {"events": [_audit_event(e) for e in rows]}


# ── Recall (owner withdraws from review) ──────────────────────────

@router.post("/{branch_id}/recall")
async def recall_branch(
    branch_id: uuid.UUID,
    session:   AsyncSession = Depends(get_session),
    user:      AuthenticatedUser = Depends(get_current_user),
):
    """Owner recalls a pending-review branch back to open for further editing."""
    b = await _get_branch_or_404(session, branch_id)

    if not user.is_admin and b.owner_id and b.owner_id != user.user_id:
        raise HTTPException(403, "Only the branch owner may recall a branch.")

    if b.status != "pending_review":
        raise HTTPException(400, f"Only 'pending_review' branches can be recalled (current: '{b.status}').")

    b.status = "open"
    b.assigned_reviewer_id = None
    b.updated_at = datetime.now(timezone.utc)
    await _log_event(session, b.id, "recalled", user)
    await session.commit()
    return _branch(b)


# ── Revert to base ─────────────────────────────────────────────────

@router.post("/{branch_id}/revert-to-base")
async def revert_to_base(
    branch_id: uuid.UUID,
    session:   AsyncSession = Depends(get_session),
    user:      AuthenticatedUser = Depends(get_current_user),
):
    """Reset branch content_snapshot to the immutable base_snapshot captured at creation."""
    b = await _get_branch_or_404(session, branch_id)

    if not user.is_admin and b.owner_id and b.owner_id != user.user_id:
        raise HTTPException(403, "Only the branch owner may revert to base.")

    if b.status not in ("open",):
        raise HTTPException(400, f"Revert to base only works on 'open' branches (current: '{b.status}'). Recall the branch first.")

    b.content_snapshot = b.base_snapshot
    b.updated_at = datetime.now(timezone.utc)
    await _log_event(session, b.id, "reverted_to_base", user)
    await session.commit()
    return _branch(b)


# ── Admin merge (emergency override) ──────────────────────────────

@router.post("/{branch_id}/merge")
async def merge_branch(
    branch_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session:   AsyncSession = Depends(get_session),
    user:      AuthenticatedUser = Depends(get_current_user),
):
    """Admin-only emergency merge. Normal flow: approval in POST /reviews auto-merges."""
    if not user.is_admin:
        raise HTTPException(403, "Only admins may trigger a manual merge. Use the review approval flow instead.")

    b = await _get_branch_or_404(session, branch_id)
    if b.status in ("merged", "closed"):
        raise HTTPException(400, f"Branch already '{b.status}'.")

    current_main: dict = {}
    if b.artifact_type and b.artifact_id:
        current_main = await _get_local_artifact(session, b.artifact_type, b.artifact_id)

    if b.branch_type == "artifact" and b.artifact_type and b.artifact_id:
        await _apply_artifact_to_main(session, b.artifact_type, b.artifact_id, b.content_snapshot)
        await _queue_rule_regen(session, background_tasks, b.artifact_type, b.artifact_id)

    now = datetime.now(timezone.utc)
    merge_diff = _compute_diff(current_main, b.content_snapshot)
    b.status = "merged"
    b.merged_by = user.user_id
    b.merged_at = now
    b.merge_diff = merge_diff
    b.updated_at = now

    await _write_merge_commit(session, b, current_main, user.username or user.user_id)
    await _log_event(session, b.id, "merged", user, {
        "via": "admin_override",
        "artifact_type": b.artifact_type,
        "artifact_id": b.artifact_id,
        "changes": merge_diff.get("total_changes", 0),
    })

    linked_story = (await session.execute(
        select(HxWorkStoryModel).where(HxWorkStoryModel.branch_id == branch_id)
    )).scalar_one_or_none()
    if linked_story and linked_story.status != "done":
        linked_story.status = "done"
        linked_story.updated_at = now

    await session.commit()
    return {**_branch(b), "merge_diff": merge_diff}


# ── Environment token management ──────────────────────────────────

@router.post("/envs/{env_id}/token")
async def set_env_token(
    env_id:  uuid.UUID,
    body:    SetEnvTokenRequest,
    session: AsyncSession = Depends(get_session),
    _:       AuthenticatedUser = Depends(get_current_user),
):
    """Store an encrypted API token for a registered environment (for live sync)."""
    env = await _get_env_or_404(session, env_id)
    env.api_token_enc = encrypt_credentials({"token": body.api_token})
    await session.commit()
    return {"env_id": str(env_id), "label": env.label, "token_set": True}


@router.post("/envs/{env_id}/test-connection")
async def test_env_connection(
    env_id:  uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _:       AuthenticatedUser = Depends(get_current_user),
):
    """Live-test the connection to a remote Helix environment."""
    env = await _get_env_or_404(session, env_id)
    if not env.url:
        raise HTTPException(400, "Environment has no URL configured.")
    token = _decrypt_token(env)

    import time
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                env.url.rstrip("/") + "/health",
                headers={"Authorization": f"Bearer {token}"},
            )
        latency_ms = round((time.monotonic() - start) * 1000)
        ok = resp.is_success or resp.status_code == 401  # 401 = reachable but token issue
        env.connection_verified_at = datetime.now(timezone.utc)
        await session.commit()
        return {
            "ok": resp.is_success,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "env_url": env.url,
            "message": "Connected" if resp.is_success else f"HTTP {resp.status_code}",
        }
    except httpx.ConnectError:
        return {"ok": False, "latency_ms": 0, "message": "Connection refused — is the remote Helix running?"}
    except httpx.TimeoutException:
        return {"ok": False, "latency_ms": 10000, "message": "Timed out after 10s"}
