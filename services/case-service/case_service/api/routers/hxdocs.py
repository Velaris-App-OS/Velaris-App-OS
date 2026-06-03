"""P58 HxDocs — Living Documentation router."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.hxdocs import service
from case_service.hxstream.emitter import emit_trace

router = APIRouter(prefix="/hxdocs", tags=["hxdocs"])


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _actor(user: AuthenticatedUser) -> str:
    return (getattr(user, "username", None)
            or getattr(user, "email", None)
            or getattr(user, "user_id", None)
            or "system")


# ── Schemas ───────────────────────────────────────────────────────────────────

class SpaceIn(BaseModel):
    name:        str
    description: str = ""
    is_public:   bool = False


class SpaceOut(BaseModel):
    id:          uuid.UUID
    name:        str
    slug:        str
    description: str | None
    is_public:   bool
    created_by:  str | None
    created_at:  str

    @classmethod
    def from_model(cls, s: Any) -> "SpaceOut":
        return cls(
            id=s.id, name=s.name, slug=s.slug, description=s.description,
            is_public=s.is_public, created_by=s.created_by,
            created_at=s.created_at.isoformat(),
        )


class ArticleIn(BaseModel):
    title:          str
    content:        list  = []
    tags:           list  = []


class ArticlePatch(BaseModel):
    title:          str | None = None
    content:        list | None = None
    tags:           list | None = None
    save_version:   bool = False


class ArticleOut(BaseModel):
    id:             uuid.UUID
    space_id:       uuid.UUID
    title:          str
    slug:           str
    status:         str
    is_public:      bool
    auto_generated: bool
    source_concept: str | None
    word_count:     int
    version:        int
    package_version:str | None
    tags:           list
    created_by:     str | None
    updated_by:     str | None
    created_at:     str
    updated_at:     str

    @classmethod
    def from_model(cls, a: Any) -> "ArticleOut":
        return cls(
            id=a.id, space_id=a.space_id, title=a.title, slug=a.slug,
            status=a.status, is_public=a.is_public,
            auto_generated=a.auto_generated, source_concept=a.source_concept,
            word_count=a.word_count, version=a.version,
            package_version=a.package_version, tags=a.tags or [],
            created_by=a.created_by, updated_by=a.updated_by,
            created_at=a.created_at.isoformat(), updated_at=a.updated_at.isoformat(),
        )


class ArticleDetail(ArticleOut):
    content: list

    @classmethod
    def from_model(cls, a: Any, content: list | None = None) -> "ArticleDetail":  # type: ignore[override]
        base = ArticleOut.from_model(a)
        return cls(**base.model_dump(), content=content if content is not None else (a.content or []))


class VersionOut(BaseModel):
    id:              uuid.UUID
    version:         int
    title:           str
    package_version: str | None
    saved_by:        str | None
    saved_at:        str

    @classmethod
    def from_model(cls, v: Any) -> "VersionOut":
        return cls(
            id=v.id, version=v.version, title=v.title,
            package_version=v.package_version, saved_by=v.saved_by,
            saved_at=v.saved_at.isoformat(),
        )


class GenerateIn(BaseModel):
    concept: str
    space_id: uuid.UUID


class LifecycleIn(BaseModel):
    case_type_id: uuid.UUID
    space_id:     uuid.UUID


class ResolveBlockIn(BaseModel):
    block: dict


class PublishIn(BaseModel):
    is_public: bool = False


# ── Spaces ────────────────────────────────────────────────────────────────────

@router.get("/spaces")
async def list_spaces(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    spaces = await service.list_spaces(session, _tenant(user))
    return [SpaceOut.from_model(s) for s in spaces]


@router.post("/spaces", status_code=201)
async def create_space(
    body: SpaceIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    space = await service.create_space(
        session, _tenant(user), body.name, body.description, body.is_public, _actor(user),
    )
    await session.commit()
    return SpaceOut.from_model(space)


@router.get("/spaces/{space_id}")
async def get_space(
    space_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    space = await service.get_space(session, space_id, _tenant(user))
    if not space:
        raise HTTPException(404, "Space not found")
    return SpaceOut.from_model(space)


# ── Articles ──────────────────────────────────────────────────────────────────

@router.get("/spaces/{space_id}/articles")
async def list_articles(
    space_id: uuid.UUID,
    status: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    space = await service.get_space(session, space_id, _tenant(user))
    if not space:
        raise HTTPException(404, "Space not found")
    articles = await service.list_articles(session, space_id, _tenant(user), status)
    return [ArticleOut.from_model(a) for a in articles]


@router.post("/spaces/{space_id}/articles", status_code=201)
async def create_article(
    space_id: uuid.UUID,
    body: ArticleIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    space = await service.get_space(session, space_id, _tenant(user))
    if not space:
        raise HTTPException(404, "Space not found")
    article = await service.create_article(
        session, space, body.title, body.content, body.tags, _actor(user),
    )
    await session.commit()
    return ArticleOut.from_model(article)


@router.get("/articles/{article_id}")
async def get_article(
    article_id: uuid.UUID,
    resolve_live: bool = Query(False),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    article = await service.get_article(session, article_id, _tenant(user))
    if not article:
        raise HTTPException(404, "Article not found")
    content = list(article.content or [])
    if resolve_live:
        content = await service.resolve_live_blocks(session, content, _tenant(user))
    return ArticleDetail.from_model(article, content)


@router.patch("/articles/{article_id}")
async def update_article(
    article_id: uuid.UUID,
    body: ArticlePatch,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    article = await service.get_article(session, article_id, _tenant(user))
    if not article:
        raise HTTPException(404, "Article not found")
    article = await service.update_article(
        session, article, body.title, body.content, body.tags,
        _actor(user), save_version=body.save_version,
    )
    await session.commit()
    await emit_trace("docs.article_updated",
                     {"article_id": str(article_id), "title": article.title},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))
    return ArticleOut.from_model(article)


@router.post("/articles/{article_id}/publish")
async def publish_article(
    article_id: uuid.UUID,
    body: PublishIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    article = await service.get_article(session, article_id, _tenant(user))
    if not article:
        raise HTTPException(404, "Article not found")
    article = await service.publish_article(session, article, body.is_public, _actor(user))
    await session.commit()
    await emit_trace("docs.article_published",
                     {"article_id": str(article_id), "is_public": body.is_public},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))
    return ArticleOut.from_model(article)


@router.delete("/articles/{article_id}", status_code=204)
async def delete_article(
    article_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    article = await service.get_article(session, article_id, _tenant(user))
    if not article:
        raise HTTPException(404, "Article not found")
    await service.delete_article(session, article)
    await session.commit()


@router.get("/articles/{article_id}/versions")
async def get_versions(
    article_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    article = await service.get_article(session, article_id, _tenant(user))
    if not article:
        raise HTTPException(404, "Article not found")
    versions = await service.get_versions(session, article_id)
    return [VersionOut.from_model(v) for v in versions]


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_articles(
    q: str = Query(..., min_length=1),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    articles = await service.search_articles(session, _tenant(user), q)
    return [ArticleOut.from_model(a) for a in articles]


# ── AI generation ─────────────────────────────────────────────────────────────

@router.post("/generate", status_code=201)
async def generate_article(
    body: GenerateIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    space = await service.get_space(session, body.space_id, _tenant(user))
    if not space:
        raise HTTPException(404, "Space not found")
    content = await service.generate_article_content(session, body.concept, _tenant(user))
    # Title is the concept (capitalised)
    title = body.concept.title()
    article = await service.create_article(
        session, space, title, content, [], _actor(user),
        auto_generated=True, source_concept=body.concept,
    )
    await session.commit()
    await emit_trace("docs.article_generated",
                     {"article_id": str(article.id), "concept": body.concept},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))
    return ArticleDetail.from_model(article)


# ── Resolve a single block ─────────────────────────────────────────────────────

@router.post("/resolve-block")
async def resolve_block(
    body: ResolveBlockIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Resolve live data for a single block — used by the editor after config change."""
    resolved = await service.resolve_live_blocks(session, [body.block], _tenant(user))
    return resolved[0] if resolved else body.block


# ── Lifecycle narrative generator ─────────────────────────────────────────────

@router.post("/generate-lifecycle", status_code=201)
async def generate_lifecycle(
    body: LifecycleIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    space = await service.get_space(session, body.space_id, _tenant(user))
    if not space:
        raise HTTPException(404, "Space not found")
    content, title = await service.generate_lifecycle_article(
        session, body.case_type_id, _tenant(user),
    )
    if content is None:
        raise HTTPException(404, "Case type not found")
    article = await service.create_article(
        session, space, title, content, ["lifecycle", "auto-generated"], _actor(user),
        auto_generated=True, source_concept=str(body.case_type_id),  # UUID stored for auto-sync
    )
    await session.commit()
    await emit_trace("docs.lifecycle_generated",
                     {"article_id": str(article.id), "case_type_id": str(body.case_type_id)},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))
    return ArticleDetail.from_model(article)


# ── Regenerate a lifecycle article ────────────────────────────────────────────

@router.post("/articles/{article_id}/regenerate-lifecycle")
async def regenerate_lifecycle(
    article_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    article = await service.get_article(session, article_id, _tenant(user))
    if not article:
        raise HTTPException(404, "Article not found")
    if not article.auto_generated or not article.source_concept:
        raise HTTPException(400, "Article is not a lifecycle guide")
    article = await service.regenerate_lifecycle_article(session, article, _actor(user))
    await session.commit()
    await emit_trace("docs.lifecycle_regenerated",
                     {"article_id": str(article_id)},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))
    content = await service.resolve_live_blocks(session, list(article.content or []), _tenant(user))
    return ArticleDetail.from_model(article, content)
