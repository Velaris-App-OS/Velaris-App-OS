"""HxGuard Phase B — tuple store helpers.

Tuples are written in the SAME transaction as the source mutation (the
caller's session) — divergence between authz state and case state is
impossible by construction. Each change also emits a thin outbox event
(`authz.tuple_changed`, ids + relation only, never case data) so a future
external authz store (Phase C / OpenFGA) has its sync feed; events with no
webhook subscribers are marked delivered immediately by the relay.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import HxGuardTupleModel, OutboxEventModel

from .service import invalidate_cache

log = logging.getLogger(__name__)

CASE_RELATIONS = {"assignee", "viewer", "editor"}


def _emit(session: AsyncSession, change: str, **fields: Any) -> None:
    session.add(OutboxEventModel(
        event_type="authz.tuple_changed",
        payload={"change": change, **{k: str(v) for k, v in fields.items()}},
    ))


async def write_tuple(
    session: AsyncSession, *,
    object_type: str, object_id: uuid.UUID, relation: str,
    subject_type: str, subject_id: str, created_by: str | None = None,
) -> None:
    """Idempotent tuple upsert (ON CONFLICT DO NOTHING), dialect-portable.

    Written in the caller's transaction, so catching IntegrityError is not an
    option (it would poison the outer txn on PG). Dispatch on the session's real
    bind dialect — in the SQLite test harness `database_backend` reads
    "postgresql" but the engine is SQLite, so config is not the source of truth.
    The conflict target is uq_hxguard_tuple (present in the model metadata, so it
    exists on every dialect's schema)."""
    values = dict(
        id=uuid.uuid4(), object_type=object_type, object_id=object_id,
        relation=relation, subject_type=subject_type, subject_id=subject_id,
        created_by=created_by,
    )
    dialect = session.get_bind().dialect.name
    if dialect == "mysql":
        stmt = mysql_insert(HxGuardTupleModel).values(**values)
        # Surgical no-op on the unique key — NOT INSERT IGNORE, which would also
        # swallow FK/truncation errors.
        stmt = stmt.on_duplicate_key_update(subject_id=stmt.inserted.subject_id)
    elif dialect == "postgresql":
        stmt = pg_insert(HxGuardTupleModel).values(**values).on_conflict_do_nothing(
            constraint="uq_hxguard_tuple"
        )
    else:  # sqlite — bare DO NOTHING resolves the uniqueness conflict
        stmt = sqlite_insert(HxGuardTupleModel).values(**values).on_conflict_do_nothing()
    await session.execute(stmt)
    _emit(session, "write", object_type=object_type, object_id=object_id,
          relation=relation, subject_type=subject_type, subject_id=subject_id)
    invalidate_cache()


async def remove_tuple(
    session: AsyncSession, *,
    object_type: str, object_id: uuid.UUID, relation: str,
    subject_type: str, subject_id: str,
) -> int:
    result = await session.execute(
        delete(HxGuardTupleModel)
        .where(HxGuardTupleModel.object_type == object_type)
        .where(HxGuardTupleModel.object_id == object_id)
        .where(HxGuardTupleModel.relation == relation)
        .where(HxGuardTupleModel.subject_type == subject_type)
        .where(HxGuardTupleModel.subject_id == subject_id)
    )
    if result.rowcount:
        _emit(session, "remove", object_type=object_type, object_id=object_id,
              relation=relation, subject_type=subject_type, subject_id=subject_id)
        invalidate_cache()
    return result.rowcount or 0


async def list_tuples(
    session: AsyncSession, *, object_type: str, object_id: uuid.UUID,
    relations: set[str] | None = None,
) -> list[HxGuardTupleModel]:
    q = (
        select(HxGuardTupleModel)
        .where(HxGuardTupleModel.object_type == object_type)
        .where(HxGuardTupleModel.object_id == object_id)
        .order_by(HxGuardTupleModel.created_at)
    )
    if relations:
        q = q.where(HxGuardTupleModel.relation.in_(relations))
    return list((await session.execute(q)).scalars().all())


async def has_tuple(
    session: AsyncSession, *,
    object_type: str, object_id: uuid.UUID, relations: set[str],
    subject_type: str, subject_id: str,
) -> str | None:
    """Return the first matching relation, or None."""
    row = (await session.execute(
        select(HxGuardTupleModel.relation)
        .where(HxGuardTupleModel.object_type == object_type)
        .where(HxGuardTupleModel.object_id == object_id)
        .where(HxGuardTupleModel.relation.in_(relations))
        .where(HxGuardTupleModel.subject_type == subject_type)
        .where(HxGuardTupleModel.subject_id == subject_id)
        .limit(1)
    )).scalar_one_or_none()
    return row
