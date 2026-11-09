"""Tenant scope on tables owned through a case — R6 soak, 2026-11-02.

`orm_scope.by_case` lost its dead `OR case_id IS NULL` branch (case_id is NOT
NULL with a foreign key in all four tables) because it forced PostgreSQL into a
quadratic plan past ~120k cases. The scope must still hold: a marked session
counts only SLA rows whose case is the caller's tenant's (or platform-shared),
never another tenant's.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from tests.conftest import TestSessionFactory, deploy_case_type
from velaris_shared.db.models import CaseInstanceModel, CaseSLAInstanceModel
from velaris_shared.tenancy import orm_scope

pytestmark = pytest.mark.asyncio


async def test_sla_rows_of_another_tenants_case_are_not_counted(client):
    ct = await deploy_case_type(client, name="Scope by case")
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with TestSessionFactory() as db:
        cases = {t: CaseInstanceModel(case_type_id=ct["id"], case_type_version="1.0.0", tenant_id=t)
                 for t in (mine, theirs, None)}
        db.add_all(cases.values())
        await db.flush()
        for t, c in cases.items():
            for _ in range(2 if t == theirs else 1):
                db.add(CaseSLAInstanceModel(case_id=c.id, sla_policy_id="p", target_id="t",
                                            started_at=now, goal_at=now + timedelta(hours=1),
                                            deadline_at=now + timedelta(hours=2)))
        await db.commit()

    async def count(uuids):
        async with TestSessionFactory() as db:
            if uuids is not None:
                orm_scope.mark(db, uuids, [])
            return (await db.execute(select(func.count()).select_from(CaseSLAInstanceModel))).scalar_one()

    assert await count(None) == 4            # unmarked: every row, as before
    assert await count([mine]) == 2          # own case + platform-shared case
    assert await count([theirs]) == 3        # their 2 + platform-shared
    assert await count([]) == 1              # scoped to nothing: platform-shared only
