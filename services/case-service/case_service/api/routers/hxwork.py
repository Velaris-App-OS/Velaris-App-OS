"""P56/P61 HxWork — Development Lifecycle Board (redesigned from Kanban)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.db.models import (
    HxWorkBoardModel, HxWorkSprintModel, HxWorkStoryModel,
    HxWorkStoryRelationModel, CaseTypeModel, ArtifactBranchModel,
)
from case_service.hxwork import service

router = APIRouter(prefix="/hxwork", tags=["hxwork"])

STORY_STATUSES = ["backlog", "in_design", "in_development", "in_review", "done"]


def _slugify(text: str) -> str:
    """Convert a story title to a branch-safe slug."""
    import re
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60]  # keep branch names reasonable length


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _actor(user: AuthenticatedUser) -> str:
    return (getattr(user, "username", None)
            or getattr(user, "email", None)
            or getattr(user, "user_id", None)
            or "system")


# ── Schemas ───────────────────────────────────────────────────────────────────

class BoardIn(BaseModel):
    name:          str
    description:   str = ""
    artifact_type: Optional[str] = None   # 'case_type'|'form'|'connector'|'rule'|...
    artifact_id:   Optional[str] = None   # UUID of the artifact this board tracks
    case_type_id:  Optional[uuid.UUID] = None   # kept for backward compat


class BoardPatch(BaseModel):
    name:          Optional[str] = None
    description:   Optional[str] = None
    artifact_id:   Optional[str] = None
    artifact_type: Optional[str] = None


class BoardOut(BaseModel):
    id:            uuid.UUID
    name:          str
    description:   str | None
    artifact_type: str | None
    artifact_id:   str | None
    case_type_id:  uuid.UUID | None
    column_config: list
    created_by:    str | None
    created_at:    str

    @classmethod
    def from_model(cls, b: Any) -> "BoardOut":
        return cls(
            id=b.id, name=b.name, description=b.description,
            artifact_type=getattr(b, "artifact_type", None),
            artifact_id=getattr(b, "artifact_id", None),
            case_type_id=b.case_type_id, column_config=b.column_config or [],
            created_by=b.created_by,
            created_at=b.created_at.isoformat(),
        )


class StoryIn(BaseModel):
    title:               str = Field(..., min_length=1, max_length=300)
    description:         Optional[str] = None
    acceptance_criteria: Optional[str] = None
    status:              str = "backlog"
    story_points:        Optional[int] = None
    assigned_to:         Optional[str] = None
    sprint_id:           Optional[uuid.UUID] = None
    artifact_type:       Optional[str] = None
    artifact_id:         Optional[str] = None


class StoryPatch(BaseModel):
    title:               Optional[str] = None
    description:         Optional[str] = None
    acceptance_criteria: Optional[str] = None
    status:              Optional[str] = None
    story_points:        Optional[int] = None
    assigned_to:         Optional[str] = None
    sprint_id:           Optional[uuid.UUID] = None
    artifact_type:       Optional[str] = None
    artifact_id:         Optional[str] = None
    branch_id:           Optional[uuid.UUID] = None
    branch_name:         Optional[str] = None


class StoryRelationIn(BaseModel):
    from_story: uuid.UUID
    to_story:   uuid.UUID
    relation:   str = "blocks"   # blocks | depends_on | relates_to


class GenerateStoriesIn(BaseModel):
    context: Optional[str] = None   # optional extra context for HxNexus


class SprintIn(BaseModel):
    name:       str
    goal:       str = ""
    start_date: datetime | None = None
    end_date:   datetime | None = None


class SprintOut(BaseModel):
    id:         uuid.UUID
    name:       str
    goal:       str | None
    status:     str
    start_date: str | None
    end_date:   str | None
    velocity:   int | None
    card_count: int = 0

    @classmethod
    def from_model(cls, s: Any, card_count: int = 0) -> "SprintOut":
        return cls(
            id=s.id, name=s.name, goal=s.goal, status=s.status,
            start_date=s.start_date.isoformat() if s.start_date else None,
            end_date=s.end_date.isoformat() if s.end_date else None,
            velocity=s.velocity,
            card_count=card_count,
        )


class CardRelationIn(BaseModel):
    from_case_id:  uuid.UUID
    to_case_id:    uuid.UUID
    relation_type: str = "blocks"


class SprintCardIn(BaseModel):
    case_id:      uuid.UUID
    story_points: int = 0


class NexusQueryIn(BaseModel):
    question: str


# ── Boards ────────────────────────────────────────────────────────────────────

@router.get("/boards", response_model=list[BoardOut])
async def list_boards(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rows = await service.list_boards(session, _tenant(user))
    return [BoardOut.from_model(b) for b in rows]


@router.post("/boards", response_model=BoardOut, status_code=201)
async def create_board(
    body: BoardIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    board = await service.create_board(
        session, _tenant(user), body.name,
        body.case_type_id, body.description, _actor(user),
    )
    # Persist artifact scope (P61)
    if body.artifact_type: board.artifact_type = body.artifact_type
    if body.artifact_id:   board.artifact_id   = body.artifact_id
    await session.commit()
    return BoardOut.from_model(board)


@router.get("/boards/{board_id}", response_model=BoardOut)
async def get_board(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    board = await service.get_board(session, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    return BoardOut.from_model(board)


@router.delete("/boards/{board_id}", status_code=204)
async def delete_board(
    board_id: uuid.UUID,
    session:  AsyncSession = Depends(get_session),
    user:     AuthenticatedUser = Depends(get_current_user),
):
    """Delete a board and all its stories, sprints, and relations."""
    board = await session.get(HxWorkBoardModel, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    await session.delete(board)
    await session.commit()


@router.patch("/boards/{board_id}", response_model=BoardOut)
async def update_board(
    board_id: uuid.UUID,
    body:     BoardPatch,
    session:  AsyncSession = Depends(get_session),
    user:     AuthenticatedUser = Depends(get_current_user),
):
    board = await session.get(HxWorkBoardModel, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    if body.name is not None:          board.name          = body.name
    if body.description is not None:   board.description   = body.description
    if body.artifact_id is not None:   board.artifact_id   = body.artifact_id
    if body.artifact_type is not None: board.artifact_type = body.artifact_type
    await session.commit()
    await session.refresh(board)
    return BoardOut.from_model(board)


@router.get("/boards/{board_id}/cards")
async def get_board_cards(
    board_id: uuid.UUID,
    sprint_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    board = await service.get_board(session, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    columns = await service.get_board_cards(session, board, sprint_id)
    return {
        "columns": board.column_config or [],
        "cards":   columns,
        "board_id": str(board_id),
    }


@router.get("/boards/{board_id}/analytics")
async def board_analytics(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    board = await service.get_board(session, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    return await service.board_analytics(session, board_id)


@router.post("/boards/{board_id}/ask")
async def ask_nexus(
    board_id: uuid.UUID,
    body: NexusQueryIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    board = await service.get_board(session, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    analytics = await service.board_analytics(session, board_id)
    answer = await service.ask_nexus(board_id, body.question, analytics)
    return {"answer": answer, "board_id": str(board_id)}


# ── Sprints ───────────────────────────────────────────────────────────────────

@router.get("/boards/{board_id}/sprints", response_model=list[SprintOut])
async def list_sprints(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    sprints = await service.list_sprints(session, board_id)
    return [SprintOut.from_model(s, card_count=len(s.cards or [])) for s in sprints]


@router.post("/boards/{board_id}/sprints", response_model=SprintOut, status_code=201)
async def create_sprint(
    board_id: uuid.UUID,
    body: SprintIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    board = await service.get_board(session, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    sprint = await service.create_sprint(
        session, board_id, _tenant(user),
        body.name, body.goal, body.start_date, body.end_date,
    )
    await session.commit()
    return SprintOut.from_model(sprint, card_count=0)


@router.post("/sprints/{sprint_id}/start", response_model=SprintOut)
async def start_sprint(
    sprint_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        sprint = await service.start_sprint(session, sprint_id)
        await session.commit()
        return SprintOut.from_model(sprint, card_count=0)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/sprints/{sprint_id}/complete", response_model=SprintOut)
async def complete_sprint(
    sprint_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        sprint = await service.complete_sprint(session, sprint_id)
        await session.commit()
        return SprintOut.from_model(sprint, card_count=0)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/sprints/{sprint_id}/cards", status_code=201)
async def add_card_to_sprint(
    sprint_id: uuid.UUID,
    body: SprintCardIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    sc = await service.add_card_to_sprint(session, sprint_id, body.case_id, body.story_points)
    await session.commit()
    return {"sprint_id": str(sprint_id), "case_id": str(body.case_id), "story_points": sc.story_points}


@router.delete("/sprints/{sprint_id}/cards/{case_id}", status_code=204)
async def remove_card_from_sprint(
    sprint_id: uuid.UUID,
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await service.remove_card_from_sprint(session, sprint_id, case_id)
    await session.commit()


# ── Card Relations ────────────────────────────────────────────────────────────

@router.post("/boards/{board_id}/relations", status_code=201)
async def add_relation(
    board_id: uuid.UUID,
    body: CardRelationIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rel = await service.add_relation(
        session, board_id, body.from_case_id, body.to_case_id, body.relation_type
    )
    await session.commit()
    return {
        "id": str(rel.id),
        "from_case_id": str(rel.from_case_id),
        "to_case_id": str(rel.to_case_id),
        "relation_type": rel.relation_type,
    }


@router.get("/boards/{board_id}/relations")
async def list_relations(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rels = await service.list_relations(session, board_id)
    return [{"id": str(r.id), "from": str(r.from_case_id), "to": str(r.to_case_id), "type": r.relation_type} for r in rels]


# ── Stories (P61 — replaces case-based cards) ────────────────────

def _story(s: HxWorkStoryModel) -> dict:
    return {
        "id":                  str(s.id),
        "board_id":            str(s.board_id),
        "sprint_id":           str(s.sprint_id) if s.sprint_id else None,
        "branch_id":           str(s.branch_id) if s.branch_id else None,
        "branch_name":         s.branch_name,
        "title":               s.title,
        "description":         s.description,
        "acceptance_criteria": s.acceptance_criteria,
        "status":              s.status,
        "story_points":        s.story_points,
        "assigned_to":         s.assigned_to,
        "linked_commit_ids":   s.linked_commit_ids or [],
        "created_by":          s.created_by,
        "created_at":          s.created_at.isoformat() if s.created_at else None,
        "updated_at":          s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/boards/{board_id}/stories")
async def list_stories(
    board_id:  uuid.UUID,
    status:    Optional[str] = None,
    sprint_id: Optional[uuid.UUID] = None,
    session:   AsyncSession = Depends(get_session),
    _:         AuthenticatedUser = Depends(get_current_user),
):
    q = select(HxWorkStoryModel).where(HxWorkStoryModel.board_id == board_id)
    if status:    q = q.where(HxWorkStoryModel.status == status)
    if sprint_id: q = q.where(HxWorkStoryModel.sprint_id == sprint_id)
    q = q.order_by(HxWorkStoryModel.created_at)
    rows = (await session.execute(q)).scalars().all()

    # Group by status column
    columns: dict[str, list] = {s: [] for s in STORY_STATUSES}
    for r in rows:
        columns.setdefault(r.status, []).append(_story(r))
    return {"columns": columns, "total": len(rows)}


@router.post("/boards/{board_id}/stories", status_code=201)
async def create_story(
    board_id: uuid.UUID,
    body:     StoryIn,
    session:  AsyncSession = Depends(get_session),
    user:     AuthenticatedUser = Depends(get_current_user),
):
    board = await session.get(HxWorkBoardModel, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    if body.status not in STORY_STATUSES:
        raise HTTPException(400, f"status must be one of {STORY_STATUSES}")

    s = HxWorkStoryModel(
        board_id=board_id, sprint_id=body.sprint_id,
        title=body.title, description=body.description,
        acceptance_criteria=body.acceptance_criteria,
        status=body.status, story_points=body.story_points,
        assigned_to=body.assigned_to, created_by=_actor(user),
    )
    session.add(s)
    await session.flush()  # get story id before branch creation

    # Story-level artifact overrides board-level artifact if provided
    effective_artifact_type = body.artifact_type or board.artifact_type
    effective_artifact_id   = body.artifact_id   or board.artifact_id

    # Auto-create a branch for this story if we have an artifact to track
    if effective_artifact_id and effective_artifact_type:
        slug = _slugify(body.title)
        branch_name = f"story/{slug}"

        # Snapshot current artifact state as base (best-effort)
        base_snapshot: dict = {}
        try:
            if effective_artifact_type == "case_type":
                ct = await session.get(CaseTypeModel, uuid.UUID(effective_artifact_id))
                if ct:
                    base_snapshot = {"id": str(ct.id), "name": ct.name,
                                     "version": ct.version, "definition_json": ct.definition_json}
            elif effective_artifact_type == "form":
                from case_service.db.models import FormDefinitionModel
                f = await session.get(FormDefinitionModel, uuid.UUID(effective_artifact_id))
                if f:
                    base_snapshot = {"id": str(f.id), "name": f.name,
                                     "version": f.version, "definition_json": f.definition_json}
            elif effective_artifact_type == "rule":
                from case_service.db.models import RuleDefinitionModel
                r = await session.get(RuleDefinitionModel, uuid.UUID(effective_artifact_id))
                if r:
                    base_snapshot = {"id": str(r.id), "name": r.name,
                                     "rule_type": r.rule_type, "definition_json": r.definition_json, "enabled": r.enabled}
            elif effective_artifact_type in ("integration", "connector"):
                from case_service.db.models import ConnectorRegistryModel
                c = await session.get(ConnectorRegistryModel, uuid.UUID(effective_artifact_id))
                if c:
                    # Never snapshot credentials
                    base_snapshot = {"id": str(c.id), "name": c.name,
                                     "connector_type": c.connector_type, "config": c.config, "enabled": c.enabled}
            elif effective_artifact_type == "escalation":
                from case_service.db.models import EscalationTreeModel
                e = await session.get(EscalationTreeModel, uuid.UUID(effective_artifact_id))
                if e:
                    base_snapshot = {"id": str(e.id), "name": e.name,
                                     "tree_json": e.tree_json, "is_active": e.is_active}
        except Exception:
            pass

        branch = ArtifactBranchModel(
            name=branch_name,
            description=f"Auto-created for story: {body.title}",
            branch_type="artifact",
            artifact_type=effective_artifact_type,
            artifact_id=effective_artifact_id,
            source_env_name="dev (story branch)",
            status="open",
            content_snapshot=base_snapshot,
            base_snapshot=base_snapshot,
            created_by=_actor(user),
        )
        session.add(branch)
        await session.flush()

        s.branch_id = branch.id
        s.branch_name = branch_name

    await session.commit()
    await session.refresh(s)
    return _story(s)


@router.patch("/boards/{board_id}/stories/{story_id}")
async def update_story(
    board_id:  uuid.UUID,
    story_id:  uuid.UUID,
    body:      StoryPatch,
    session:   AsyncSession = Depends(get_session),
    user:      AuthenticatedUser = Depends(get_current_user),
):
    s = await session.get(HxWorkStoryModel, story_id)
    if not s or s.board_id != board_id:
        raise HTTPException(404, "Story not found")

    old_status = s.status
    if body.title is not None:               s.title               = body.title
    if body.description is not None:         s.description         = body.description
    if body.acceptance_criteria is not None: s.acceptance_criteria = body.acceptance_criteria
    if body.status is not None:
        if body.status not in STORY_STATUSES:
            raise HTTPException(400, f"status must be one of {STORY_STATUSES}")
        s.status = body.status
    if body.story_points is not None: s.story_points = body.story_points
    if body.assigned_to is not None:  s.assigned_to  = body.assigned_to
    if body.sprint_id is not None:    s.sprint_id    = body.sprint_id
    if body.branch_id is not None:
        # Verify the branch exists before linking
        branch = await session.get(ArtifactBranchModel, body.branch_id)
        if not branch:
            raise HTTPException(404, "Branch not found")
        s.branch_id   = branch.id
        s.branch_name = body.branch_name or branch.name
    if body.branch_name is not None and body.branch_id is None:
        s.branch_name = body.branch_name
    s.updated_at = datetime.now(timezone.utc)

    # Sync branch lifecycle with story status transitions
    if s.branch_id and body.status and body.status != old_status:
        branch = await session.get(ArtifactBranchModel, s.branch_id)
        if branch:
            if body.status == "in_review" and branch.status == "open":
                # Story enters review → auto-submit branch for review
                branch.status = "pending_review"
                branch.updated_at = datetime.now(timezone.utc)
            elif body.status == "done" and branch.status not in ("merged", "closed"):
                # Manually marking done without merge → close branch
                branch.status = "closed"
                branch.updated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(s)
    return _story(s)


@router.delete("/boards/{board_id}/stories/{story_id}", status_code=204)
async def delete_story(
    board_id: uuid.UUID,
    story_id: uuid.UUID,
    session:  AsyncSession = Depends(get_session),
    _:        AuthenticatedUser = Depends(get_current_user),
):
    s = await session.get(HxWorkStoryModel, story_id)
    if not s or s.board_id != board_id:
        raise HTTPException(404, "Story not found")
    await session.delete(s)
    await session.commit()


@router.post("/boards/{board_id}/generate-stories", status_code=201)
async def generate_stories(
    board_id: uuid.UUID,
    body:     GenerateStoriesIn,
    session:  AsyncSession = Depends(get_session),
    user:     AuthenticatedUser = Depends(get_current_user),
):
    """Use HxNexus to generate draft user stories for this board's artifact."""
    board = await session.get(HxWorkBoardModel, board_id)
    if not board:
        raise HTTPException(404, "Board not found")

    # Gather artifact context
    artifact_context = body.context or ""
    if board.artifact_type == "case_type" and board.artifact_id:
        try:
            ct = await session.get(CaseTypeModel, uuid.UUID(board.artifact_id))
            if ct:
                import json as _json
                artifact_context = (
                    f"Case type: {ct.name}\nDescription: {ct.description or ''}\n"
                    f"Definition: {_json.dumps(ct.definition_json or {})[:1500]}"
                )
        except Exception:
            pass

    from case_service.hxnexus.factory import generate_json
    prompt = (
        f"Generate user stories for this Velaris artifact:\n{artifact_context}\n\n"
        "Create 5-8 user stories covering: happy path, edge cases, error handling, SLA compliance, and admin use cases.\n"
        'Return JSON: {"stories": [{"title": "...", "description": "...", "acceptance_criteria": "...", "story_points": 3}]}'
    )
    result = await generate_json(prompt, system="You are an Agile BA generating user stories for a BPM platform.")
    if not result or "stories" not in result:
        raise HTTPException(503, "HxNexus unavailable — cannot generate stories. Create them manually.")

    created = []
    for raw in result["stories"][:10]:
        s = HxWorkStoryModel(
            board_id=board_id,
            title=raw.get("title", "Untitled story")[:300],
            description=raw.get("description"),
            acceptance_criteria=raw.get("acceptance_criteria"),
            status="backlog",
            story_points=raw.get("story_points"),
            created_by=_actor(user),
        )
        session.add(s)
        created.append(s)

    await session.commit()
    for s in created:
        await session.refresh(s)
    return {"generated": len(created), "stories": [_story(s) for s in created]}


# ── Story relations ───────────────────────────────────────────────

@router.post("/boards/{board_id}/story-relations", status_code=201)
async def add_story_relation(
    board_id: uuid.UUID,
    body:     StoryRelationIn,
    session:  AsyncSession = Depends(get_session),
    _:        AuthenticatedUser = Depends(get_current_user),
):
    rel = HxWorkStoryRelationModel(
        board_id=board_id,
        from_story=body.from_story,
        to_story=body.to_story,
        relation=body.relation,
    )
    session.add(rel)
    await session.commit()
    await session.refresh(rel)
    return {"id": str(rel.id), "from": str(rel.from_story),
            "to": str(rel.to_story), "relation": rel.relation}


@router.get("/boards/{board_id}/story-relations")
async def list_story_relations(
    board_id: uuid.UUID,
    session:  AsyncSession = Depends(get_session),
    _:        AuthenticatedUser = Depends(get_current_user),
):
    rows = (await session.execute(
        select(HxWorkStoryRelationModel).where(HxWorkStoryRelationModel.board_id == board_id)
    )).scalars().all()
    return [{"id": str(r.id), "from": str(r.from_story), "to": str(r.to_story), "relation": r.relation} for r in rows]


@router.delete("/story-relations/{relation_id}", status_code=204)
async def delete_story_relation(
    relation_id: uuid.UUID,
    session:     AsyncSession = Depends(get_session),
    _:           AuthenticatedUser = Depends(get_current_user),
):
    r = await session.get(HxWorkStoryRelationModel, relation_id)
    if not r:
        raise HTTPException(404, "Relation not found")
    await session.delete(r)
    await session.commit()
