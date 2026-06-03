"""P61 — Platform-wide Commit pattern.

Every save action in Helix is a Commit — a named, auditable change
with a mandatory message. Commits are recorded here and trigger
HxWork story advancement via HxNexus NLP.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import ComponentCommitModel, HxWorkStoryModel, HxWorkBoardModel
from case_service.db.session import get_session
from case_service.auth.dependencies import get_current_user
from case_service.hxstream.emitter import emit_trace
from case_service.auth.models import AuthenticatedUser

router = APIRouter(prefix="/commits", tags=["commits"])

STORY_STATUSES = ["backlog", "in_design", "in_development", "in_review", "done"]

# Signal words mapped to target status
_ADVANCE_SIGNALS: list[tuple[list[str], str]] = [
    (["design", "planning", "spec", "drafted", "wireframe", "schema design", "proposal"], "in_design"),
    (["implemented", "built", "added", "wired", "created", "developed", "coded", "wrote"], "in_development"),
    (["fixed", "resolved", "updated", "refactored", "improved", "corrected"], "in_development"),
    (["ready for review", "pr open", "pull request", "submitted for review", "review ready", "testing"], "in_review"),
    (["merged", "completed", "done", "approved", "closed", "deployed", "released", "shipped"], "done"),
]


class RecordCommitRequest(BaseModel):
    component_type:  str  = Field(..., description="case_type | form | connector | rule | portal | escalation | region | process | canvas | docs | ...")
    component_id:    str  = Field(..., description="UUID of the saved component")
    component_name:  str  = Field(..., description="Display name of the component")
    commit_message:  str  = Field(..., min_length=10, max_length=500)
    diff_snapshot:   Optional[dict] = None


def _serialise(c: ComponentCommitModel) -> dict:
    return {
        "id":             str(c.id),
        "component_type": c.component_type,
        "component_id":   c.component_id,
        "component_name": c.component_name,
        "commit_message": c.commit_message,
        "committed_by":   c.committed_by,
        "diff_snapshot":  c.diff_snapshot,
        "story_matches":  c.story_matches,
        "committed_at":   c.committed_at.isoformat() if c.committed_at else None,
    }


def _infer_target_status(message: str) -> str | None:
    """Return the story status the commit message implies, or None if unclear."""
    lower = message.lower()
    for signals, status in _ADVANCE_SIGNALS:
        if any(sig in lower for sig in signals):
            return status
    return None


async def _advance_stories(
    session: AsyncSession,
    commit_id: uuid.UUID,
    component_type: str,
    component_id: str,
    commit_message: str,
    committed_by: str,
) -> list[dict]:
    """Find the board for this artifact and advance matching stories via NLP."""
    # Find board scoped to this artifact
    board_row = (await session.execute(
        select(HxWorkBoardModel).where(
            HxWorkBoardModel.artifact_type == component_type,
            HxWorkBoardModel.artifact_id == component_id,
        ).limit(1)
    )).scalar_one_or_none()

    if not board_row:
        return []

    target_status = _infer_target_status(commit_message)
    if not target_status:
        # Try HxNexus for semantic inference
        try:
            from case_service.hxnexus.factory import generate_json
            stories_q = (await session.execute(
                select(HxWorkStoryModel)
                .where(HxWorkStoryModel.board_id == board_row.id)
                .where(HxWorkStoryModel.status != "done")
            )).scalars().all()

            if stories_q:
                titles_str = "\n".join(f"- {s.title} (current: {s.status})" for s in stories_q)
                prompt = (
                    f'Commit message: "{commit_message}"\n\n'
                    f"User stories on this board:\n{titles_str}\n\n"
                    'Return JSON: {"matches": [{"title": "...", "target_status": "in_design|in_development|in_review|done"}]}\n'
                    'Only include stories that clearly relate to the commit. target_status must only advance (never go backwards). '
                    'Valid statuses in order: backlog, in_design, in_development, in_review, done.'
                )
                result = await generate_json(prompt, system="You are a development lifecycle assistant.")
                if result and "matches" in result:
                    matched: list[dict] = []
                    for m in result["matches"]:
                        story = next((s for s in stories_q if s.title == m.get("title")), None)
                        ts = m.get("target_status")
                        if story and ts and ts in STORY_STATUSES:
                            curr_idx = STORY_STATUSES.index(story.status) if story.status in STORY_STATUSES else 0
                            tgt_idx  = STORY_STATUSES.index(ts)
                            if tgt_idx > curr_idx:
                                old_status = story.status
                                story.status = ts
                                story.linked_commit_ids = [*story.linked_commit_ids, str(commit_id)]
                                story.updated_at = datetime.now(timezone.utc)
                                matched.append({"story_id": str(story.id), "title": story.title,
                                                "from_status": old_status, "to_status": ts})
                    await session.commit()
                    return matched
        except Exception:
            pass
        return []

    # Keyword-based advancement — match stories semantically by title keywords
    stories = (await session.execute(
        select(HxWorkStoryModel)
        .where(HxWorkStoryModel.board_id == board_row.id)
        .where(HxWorkStoryModel.status != "done")
    )).scalars().all()

    matched: list[dict] = []
    msg_words = set(commit_message.lower().split())
    for story in stories:
        # Check word overlap between commit message and story title
        story_words = set(story.title.lower().split())
        overlap = msg_words & story_words - {"the", "a", "an", "and", "or", "to", "of", "in", "for", "with", "is"}
        if len(overlap) >= 2:
            curr_idx = STORY_STATUSES.index(story.status) if story.status in STORY_STATUSES else 0
            tgt_idx  = STORY_STATUSES.index(target_status)
            if tgt_idx > curr_idx:
                old_status = story.status
                story.status = target_status
                story.linked_commit_ids = [*story.linked_commit_ids, str(commit_id)]
                story.updated_at = datetime.now(timezone.utc)
                matched.append({"story_id": str(story.id), "title": story.title,
                                "from_status": old_status, "to_status": target_status})

    if matched:
        await session.commit()
    return matched


# ── Endpoints ─────────────────────────────────────────────────────

@router.post("", status_code=201)
async def record_commit(
    body:       RecordCommitRequest,
    background: BackgroundTasks,
    session:    AsyncSession = Depends(get_session),
    user:       AuthenticatedUser = Depends(get_current_user),
):
    """Record a commit for any saved component and trigger story advancement."""
    commit = ComponentCommitModel(
        component_type=body.component_type,
        component_id=body.component_id,
        component_name=body.component_name,
        commit_message=body.commit_message,
        committed_by=user.username or user.user_id,
        diff_snapshot=body.diff_snapshot,
    )
    session.add(commit)
    await session.flush()
    commit_id = commit.id

    # Advance stories (inline — fast enough for most cases)
    story_matches = await _advance_stories(
        session, commit_id,
        body.component_type, body.component_id,
        body.commit_message, user.username or user.user_id,
    )

    commit.story_matches = story_matches or None
    await session.commit()
    await session.refresh(commit)

    # Broadcast to HxStream so it appears in the live feed
    await emit_trace(
        session,
        event_type="commit",
        actor_user_id=user.user_id,
        payload={
            "commit_id":      str(commit.id),
            "component_type": body.component_type,
            "component_id":   body.component_id,
            "component_name": body.component_name,
            "message":        body.commit_message,
            "stories_advanced": len(story_matches),
            "story_matches":  story_matches,
        },
    )

    return _serialise(commit)


@router.get("")
async def list_commits(
    component_type: Optional[str] = None,
    component_id:   Optional[str] = None,
    committed_by:   Optional[str] = None,
    limit:          int = 50,
    session:        AsyncSession = Depends(get_session),
    _:              AuthenticatedUser = Depends(get_current_user),
):
    q = select(ComponentCommitModel).order_by(desc(ComponentCommitModel.committed_at)).limit(limit)
    if component_type: q = q.where(ComponentCommitModel.component_type == component_type)
    if component_id:   q = q.where(ComponentCommitModel.component_id   == component_id)
    if committed_by:   q = q.where(ComponentCommitModel.committed_by   == committed_by)
    rows = (await session.execute(q)).scalars().all()
    return {"commits": [_serialise(r) for r in rows], "total": len(rows)}


@router.get("/{commit_id}")
async def get_commit(
    commit_id: uuid.UUID,
    session:   AsyncSession = Depends(get_session),
    _:         AuthenticatedUser = Depends(get_current_user),
):
    c = await session.get(ComponentCommitModel, commit_id)
    if not c:
        from fastapi import HTTPException
        raise HTTPException(404, "Commit not found")
    return _serialise(c)
