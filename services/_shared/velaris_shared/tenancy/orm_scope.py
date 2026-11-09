"""Tenant scope for a whole READ session — 8d, 2026-09-29.

analytics-service had no tenant scoping at all: the dashboard, the metrics
snapshot, the funnel, the NL and structured query engine, saved reports, the
OData feed PowerBI connects to, and process mining all counted and listed every
tenant's cases for any caller with `analytics.reports.read`. Its queries are
spread over the router, `velaris_shared.analytics.*` and the mining module,
and they are all ORM. So instead of threading a tenant through forty query
builders, a request marks its session with the caller's tenant and one ORM
event adds the criteria to every SELECT that touches a case, or a table owned
through a case:

* `CaseInstanceModel`, `CaseTypeModel`, `SavedReportModel`, `TraceEventModel`
  — their own `tenant_id` is the caller's, or NULL (platform-shared, as
  `same_tenant` has always treated it);
* SLA instances, assignments, the audit log and the process-mining event log
  — their `case_id` is one of those cases.

A session nobody marked is untouched, so every other service, every background
job and superadmin behave exactly as before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import event, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

log = logging.getLogger(__name__)

SCOPE_KEY = "velaris_tenant_scope"


def mark(session: Any, uuids: list[uuid.UUID], tokens: list[str]) -> None:
    """Scope every later SELECT on this session to these tenant spellings."""
    target = getattr(session, "sync_session", session)
    target.info[SCOPE_KEY] = {"uuids": list(uuids), "tokens": list(tokens)}


def _criteria(scope: dict) -> list:
    from velaris_shared.db import models as M

    uuids, tokens = scope["uuids"], scope["tokens"]

    def own_guid(cls):
        return or_(cls.tenant_id.in_(uuids), cls.tenant_id.is_(None))

    def own_str(cls):
        return or_(cls.tenant_id.in_(tokens), cls.tenant_id.is_(None))

    cases = select(M.CaseInstanceModel.id).where(
        or_(M.CaseInstanceModel.tenant_id.in_(uuids), M.CaseInstanceModel.tenant_id.is_(None)))

    # `case_id IN (the caller's cases)` and nothing else (R6 soak, 2026-11-02).
    # It used to add `OR case_id IS NULL`. That branch can never match —
    # case_id is NOT NULL with a foreign key in all four tables, on PostgreSQL
    # and MySQL — but it stopped PostgreSQL from planning a hash join: past
    # work_mem (≈120k cases) it re-read every case for every SLA row, an SLA
    # count ran for over 8 minutes, and every analytics page timed out. Without
    # it the plan is one hash join, and a row whose case is not the caller's
    # (or does not exist) is still excluded — fail closed, as before.
    def by_case(cls):
        return cls.case_id.in_(cases)

    out = [
        with_loader_criteria(M.CaseInstanceModel, own_guid, include_aliases=True),
        with_loader_criteria(M.CaseTypeModel, own_guid, include_aliases=True),
        with_loader_criteria(M.SavedReportModel, own_str, include_aliases=True),
        with_loader_criteria(M.TraceEventModel, own_str, include_aliases=True),
    ]
    for model in (M.CaseSLAInstanceModel, M.CaseAssignmentModel,
                  M.CaseAuditLogModel, M.CaseEventLogModel):
        out.append(with_loader_criteria(model, by_case, include_aliases=True))
    return out


@event.listens_for(Session, "do_orm_execute")
def _apply(state) -> None:
    scope = state.session.info.get(SCOPE_KEY)
    if scope is None or not state.is_select:
        return
    state.statement = state.statement.options(*_criteria(scope))


async def staff_caller(request) -> Any:
    """The staff caller of this request, or None (the route's auth decides)."""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from velaris_shared.auth.dependencies import get_current_user, is_auth_check_busy

    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=header[7:].strip())
    try:
        return await get_current_user(request, creds)
    except HTTPException as exc:
        # A busy database is "try again", not "no caller": scoped to nothing,
        # the page would show an empty list as though the data were gone.
        if is_auth_check_busy(exc):
            raise
        return None


async def scope_session_to_caller(request, *sessions) -> None:
    """Mark these sessions for this request's caller. superadmin is unscoped."""
    from velaris_shared.tenancy.identity import (
        caller_tenant_tokens, is_superadmin, resolve_tenant,
    )

    if not sessions:
        return
    user = await staff_caller(request)
    if user is None:
        # Fail CLOSED. A request that carries a token we could not resolve here
        # may still pass the route's own (separately evaluated) authentication,
        # e.g. across a revocation-cache refresh — and an unmarked session reads
        # every tenant. Scope it to nothing but platform-shared rows instead.
        if (request.headers.get("authorization") or "").lower().startswith("bearer "):
            for s in sessions:
                mark(s, [], [])
        return
    if is_superadmin(user):
        return
    tokens = await caller_tenant_tokens(sessions[0], user) or []
    uuids: list[uuid.UUID] = []
    for t in tokens:
        try:
            uuids.append(uuid.UUID(str(t)))
        except ValueError:
            resolved = await resolve_tenant(sessions[0], str(t))
            if resolved is not None:
                uuids.append(resolved[0])
    for s in sessions:
        mark(s, uuids, tokens)
