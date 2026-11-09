"""The case list's visibility filter — every way in, and the ways out (R6 soak).

2026-11-02: `search_case_instances(accessible_to_user=…)` was rewritten from
`created_by = u OR EXISTS(user tuple) OR EXISTS(group tuple) OR type IN (…)`,
which PostgreSQL could only answer by reading every case in the tenant, to
`id IN (one indexed lookup per way in)`. The set of cases must be exactly the
same. Each branch is proven here by a case that ONLY that branch admits, and
the refusals by cases nothing admits (an unrelated creator, a tuple for
someone else, a relation that is not a read grant, a group the user is not in,
an inactive group, another tenant).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import TestSessionFactory, deploy_case_type
from velaris_shared.db.models import (
    AccessGroupModel, CaseInstanceModel, HxGuardTupleModel, OperatorAccessGroupModel, PortalModel,
)
from velaris_shared.db.repository import search_case_instances

pytestmark = pytest.mark.asyncio

ME = "user-me"
OTHER = "user-other"


async def _world(client):
    ct = await deploy_case_type(client, name="Visibility shape")
    ct2 = await deploy_case_type(client, name="Visibility type grant")
    tenant = uuid.uuid4()
    async with TestSessionFactory() as db:
        portal = PortalModel(name="p", portal_type="staff")
        db.add(portal)
        await db.flush()
        mine = AccessGroupModel(name="mine", tenant_id="default", portal_id=portal.id)
        dead = AccessGroupModel(name="dead", tenant_id="default", portal_id=portal.id, is_active=False)
        theirs = AccessGroupModel(name="theirs", tenant_id="default", portal_id=portal.id)
        db.add_all([mine, dead, theirs])
        await db.flush()
        db.add_all([OperatorAccessGroupModel(operator_id=ME, access_group_id=mine.id),
                    OperatorAccessGroupModel(operator_id=ME, access_group_id=dead.id),
                    OperatorAccessGroupModel(operator_id=OTHER, access_group_id=theirs.id)])

        def case(label, created_by=OTHER, type_id=ct["id"], tenant_id=tenant):
            c = CaseInstanceModel(case_type_id=type_id, case_type_version="1.0.0", created_by=created_by,
                                  tenant_id=tenant_id, data={"label": label})
            db.add(c)
            return c

        made = case("created", created_by=ME)
        user_tuple = case("user tuple")
        group_tuple = case("group tuple")
        by_type = case("type grant", type_id=ct2["id"])
        # Nothing admits these.
        case("unrelated")
        other_user = case("tuple for someone else")
        wrong_rel = case("non-read relation")
        dead_group = case("inactive group")
        their_group = case("group I am not in")
        case("mine, other tenant", created_by=ME, tenant_id=uuid.uuid4())
        deleted = case("mine, deleted", created_by=ME)
        deleted.is_deleted = True
        await db.flush()

        def tup(obj, subject_type, subject_id, relation="viewer"):
            db.add(HxGuardTupleModel(object_type="case", object_id=obj.id, relation=relation,
                                     subject_type=subject_type, subject_id=subject_id))

        tup(user_tuple, "user", ME, "assignee")
        tup(group_tuple, "access_group", str(mine.id), "editor")
        tup(other_user, "user", OTHER)
        tup(wrong_rel, "user", ME, "owner")
        tup(dead_group, "access_group", str(dead.id))
        tup(their_group, "access_group", str(theirs.id))
        await db.commit()
        return tenant, ct2["id"], {made.id, user_tuple.id, group_tuple.id}, by_type.id


async def _list(tenant, allowed):
    async with TestSessionFactory() as db:
        rows, total = await search_case_instances(
            db, filters={}, limit=100, tenant_id=tenant, include_untenanted=False,
            accessible_to_user=(ME, allowed))
    return {r.id for r in rows}, total


async def test_each_way_in_admits_exactly_its_case(client):
    tenant, type2, relational, by_type = await _world(client)
    ids, total = await _list(tenant, [])
    assert ids == relational and total == 3
    ids, total = await _list(tenant, [type2])
    assert ids == relational | {by_type} and total == 4


async def test_a_user_with_no_relationship_sees_nothing(client):
    tenant, _, _, _ = await _world(client)
    async with TestSessionFactory() as db:
        rows, total = await search_case_instances(
            db, filters={}, limit=100, tenant_id=tenant, include_untenanted=False,
            accessible_to_user=("user-stranger", []))
    assert rows == [] and total == 0


async def test_a_case_two_ways_in_is_listed_once(client):
    tenant, _, relational, _ = await _world(client)
    made = next(iter(relational))
    async with TestSessionFactory() as db:
        db.add(HxGuardTupleModel(object_type="case", object_id=made, relation="viewer",
                                 subject_type="user", subject_id=ME))
        await db.commit()
    ids, total = await _list(tenant, [])
    assert ids == relational and total == 3
