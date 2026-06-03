"""Phase 8 tests — Soft-Delete, Business Calendar SLAs, ABAC.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


# ═══════════════════════════════════════════════════════════════════
# Business Calendar Tests
# ═══════════════════════════════════════════════════════════════════

class TestBusinessCalendar:
    def _cal(self, **kw):
        from case_service.core.business_calendar import BusinessCalendar
        defaults = dict(
            name="test",
            work_days=[1, 2, 3, 4, 5],  # Mon-Fri
            work_start_hour=9,
            work_end_hour=17,
            holidays=[],
        )
        defaults.update(kw)
        return BusinessCalendar(**defaults)

    def test_is_work_day_weekday(self):
        cal = self._cal()
        # 2026-04-13 is Monday
        assert cal.is_work_day(datetime(2026, 4, 13).date()) is True

    def test_is_work_day_weekend(self):
        cal = self._cal()
        # 2026-04-11 is Saturday
        assert cal.is_work_day(datetime(2026, 4, 11).date()) is False
        # 2026-04-12 is Sunday
        assert cal.is_work_day(datetime(2026, 4, 12).date()) is False

    def test_is_work_day_holiday(self):
        cal = self._cal(holidays=["2026-04-13"])
        assert cal.is_work_day(datetime(2026, 4, 13).date()) is False

    def test_is_work_time(self):
        cal = self._cal()
        mon_10am = datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc)
        mon_7am = datetime(2026, 4, 13, 7, 0, tzinfo=timezone.utc)
        mon_18pm = datetime(2026, 4, 13, 18, 0, tzinfo=timezone.utc)
        assert cal.is_work_time(mon_10am) is True
        assert cal.is_work_time(mon_7am) is False
        assert cal.is_work_time(mon_18pm) is False

    def test_add_business_duration_same_day(self):
        cal = self._cal()
        start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)  # Mon 9am
        result = cal.add_business_duration(start, timedelta(hours=4))
        assert result == datetime(2026, 4, 13, 13, 0, tzinfo=timezone.utc)  # Mon 1pm

    def test_add_business_duration_spans_weekend(self):
        cal = self._cal()
        # Friday 4pm + 4 business hours
        start = datetime(2026, 4, 10, 16, 0, tzinfo=timezone.utc)  # Fri 4pm
        result = cal.add_business_duration(start, timedelta(hours=4))
        # 1h left Friday (4-5pm), then 3h Monday = Mon 12pm
        assert result.weekday() == 0  # Monday
        assert result.hour == 12

    def test_add_business_duration_starts_outside_hours(self):
        cal = self._cal()
        start = datetime(2026, 4, 13, 20, 0, tzinfo=timezone.utc)  # Mon 8pm
        result = cal.add_business_duration(start, timedelta(hours=2))
        # Should start counting from Tue 9am
        assert result == datetime(2026, 4, 14, 11, 0, tzinfo=timezone.utc)

    def test_add_business_duration_skips_holiday(self):
        cal = self._cal(holidays=["2026-04-14"])  # Tuesday is holiday
        start = datetime(2026, 4, 13, 16, 0, tzinfo=timezone.utc)  # Mon 4pm
        result = cal.add_business_duration(start, timedelta(hours=4))
        # 1h Mon, skip Tue (holiday), 3h Wed = Wed 12pm
        assert result.day == 15  # Wednesday
        assert result.hour == 12

    def test_business_seconds_between(self):
        cal = self._cal()
        start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)  # Mon 9am
        end = datetime(2026, 4, 13, 17, 0, tzinfo=timezone.utc)  # Mon 5pm
        assert cal.business_seconds_between(start, end) == 8 * 3600

    def test_business_seconds_across_weekend(self):
        cal = self._cal()
        fri_5pm = datetime(2026, 4, 10, 17, 0, tzinfo=timezone.utc)
        mon_9am = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        # Weekend = 0 business seconds
        assert cal.business_seconds_between(fri_5pm, mon_9am) == 0

    def test_24x7_calendar(self):
        from case_service.core.business_calendar import BusinessCalendar
        cal = BusinessCalendar.twenty_four_seven()
        sat = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)
        assert cal.is_work_day(sat.date()) is True
        result = cal.add_business_duration(sat, timedelta(hours=2))
        assert result == datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# ABAC Access Control Tests
# ═══════════════════════════════════════════════════════════════════

class TestABAC:
    def test_role_based_allow(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {
            "roles": [
                {"id": "admin", "name": "Admin", "permissions": ["case:create", "case:read"]},
            ],
            "policies": [],
        }
        user = UserContext(user_id="u1", roles=["admin"])
        resource = ResourceContext(resource_type="case")
        decision = evaluate_access(profile, user, resource, "case:create")
        assert decision.allowed is True

    def test_role_based_deny(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {
            "roles": [
                {"id": "viewer", "name": "Viewer", "permissions": ["case:read"]},
            ],
            "policies": [],
        }
        user = UserContext(user_id="u1", roles=["viewer"])
        resource = ResourceContext(resource_type="case")
        decision = evaluate_access(profile, user, resource, "case:create")
        assert decision.allowed is False

    def test_abac_policy_allow(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {
            "roles": [],
            "policies": [
                {
                    "id": "p1", "name": "Dept Match",
                    "effect": "allow",
                    "permissions": ["case:read"],
                    "conditions": [
                        {"attribute": "user.department", "operator": "eq", "value": "engineering"},
                    ],
                    "priority": 10,
                },
            ],
        }
        user = UserContext(user_id="u1", department="engineering")
        resource = ResourceContext(resource_type="case")
        decision = evaluate_access(profile, user, resource, "case:read")
        assert decision.allowed is True
        assert decision.matched_policy == "p1"

    def test_abac_policy_deny(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {
            "roles": [
                {"id": "user", "name": "User", "permissions": ["case:read"]},
            ],
            "policies": [
                {
                    "id": "p1", "name": "Block Critical",
                    "effect": "deny",
                    "permissions": ["case:read"],
                    "conditions": [
                        {"attribute": "resource.priority", "operator": "eq", "value": "critical"},
                    ],
                    "priority": 100,
                },
            ],
        }
        user = UserContext(user_id="u1", roles=["user"])
        resource = ResourceContext(resource_type="case", priority="critical")
        decision = evaluate_access(profile, user, resource, "case:read")
        assert decision.allowed is False

    def test_abac_condition_in(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {
            "roles": [],
            "policies": [
                {
                    "id": "p1", "name": "Role In List",
                    "effect": "allow",
                    "permissions": ["case:update"],
                    "conditions": [
                        {"attribute": "user.department", "operator": "in", "value": ["engineering", "sales"]},
                    ],
                },
            ],
        }
        user = UserContext(user_id="u1", department="sales")
        resource = ResourceContext(resource_type="case")
        assert evaluate_access(profile, user, resource, "case:update").allowed is True

        user2 = UserContext(user_id="u2", department="hr")
        assert evaluate_access(profile, user2, resource, "case:update").allowed is False

    def test_abac_multiple_conditions_and(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {
            "roles": [],
            "policies": [
                {
                    "id": "p1", "name": "Dept + Status",
                    "effect": "allow",
                    "permissions": ["case:close"],
                    "conditions": [
                        {"attribute": "user.department", "operator": "eq", "value": "engineering"},
                        {"attribute": "resource.status", "operator": "eq", "value": "resolved"},
                    ],
                },
            ],
        }
        user = UserContext(user_id="u1", department="engineering")
        resource_ok = ResourceContext(resource_type="case", status="resolved")
        resource_bad = ResourceContext(resource_type="case", status="open")
        assert evaluate_access(profile, user, resource_ok, "case:close").allowed is True
        assert evaluate_access(profile, user, resource_bad, "case:close").allowed is False

    def test_abac_priority_order(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {
            "roles": [],
            "policies": [
                {
                    "id": "p1", "name": "Low Priority Allow",
                    "effect": "allow", "permissions": ["case:read"],
                    "conditions": [], "priority": 1,
                },
                {
                    "id": "p2", "name": "High Priority Deny",
                    "effect": "deny", "permissions": ["case:read"],
                    "conditions": [], "priority": 100,
                },
            ],
        }
        user = UserContext(user_id="u1")
        resource = ResourceContext(resource_type="case")
        decision = evaluate_access(profile, user, resource, "case:read")
        assert decision.allowed is False
        assert decision.matched_policy == "p2"

    def test_field_access(self):
        from case_service.core.access_control import (
            UserContext, get_field_access,
        )
        profile = {
            "field_access": [
                {"field_id": "ssn", "role_id": "admin", "readable": True, "writable": False, "masked": False},
                {"field_id": "ssn", "role_id": "viewer", "readable": True, "writable": False, "masked": True},
                {"field_id": "name", "readable": True, "writable": True, "masked": False},
            ],
        }
        admin = UserContext(user_id="u1", roles=["admin"])
        viewer = UserContext(user_id="u2", roles=["viewer"])

        admin_fields = get_field_access(profile, admin)
        assert admin_fields["ssn"]["readable"] is True
        assert admin_fields["ssn"]["masked"] is False

        viewer_fields = get_field_access(profile, viewer)
        assert viewer_fields["ssn"]["readable"] is True
        assert viewer_fields["ssn"]["masked"] is True

    def test_default_deny_no_policy(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {"roles": [], "policies": []}
        user = UserContext(user_id="u1")
        resource = ResourceContext(resource_type="case")
        decision = evaluate_access(profile, user, resource, "case:read")
        assert decision.allowed is False

    def test_wildcard_permission(self):
        from case_service.core.access_control import (
            UserContext, ResourceContext, evaluate_access,
        )
        profile = {
            "roles": [],
            "policies": [
                {"id": "p1", "name": "Super Admin", "effect": "allow",
                 "permissions": ["*"], "conditions": [
                     {"attribute": "user.department", "operator": "eq", "value": "admin"},
                 ]},
            ],
        }
        user = UserContext(user_id="u1", department="admin")
        resource = ResourceContext(resource_type="case")
        assert evaluate_access(profile, user, resource, "anything:here").allowed is True


# ═══════════════════════════════════════════════════════════════════
# Soft-Delete API Tests
# ═══════════════════════════════════════════════════════════════════

class TestSoftDelete:
    async def test_soft_delete_case_type(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"SoftDel-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-process",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        assert ct.status_code == 201
        ct_id = ct.json()["id"]

        # Soft delete
        resp = await client.delete(f"/api/v1/case-types/{ct_id}")
        assert resp.status_code == 204

        # Should not appear in list
        list_resp = await client.get("/api/v1/case-types")
        ids = [item["id"] for item in list_resp.json()["items"]]
        assert ct_id not in ids

        # But should still be fetchable directly
        get_resp = await client.get(f"/api/v1/case-types/{ct_id}")
        assert get_resp.status_code == 200

    async def test_restore_case_type(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"Restore-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-process",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        ct_id = ct.json()["id"]

        # Delete then restore
        await client.delete(f"/api/v1/case-types/{ct_id}")
        restore_resp = await client.post(f"/api/v1/case-types/{ct_id}/restore")
        assert restore_resp.status_code == 200

        # Should appear in list again
        list_resp = await client.get("/api/v1/case-types")
        ids = [item["id"] for item in list_resp.json()["items"]]
        assert ct_id in ids

    async def test_restore_non_deleted_fails(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"NotDel-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-process",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        ct_id = ct.json()["id"]

        resp = await client.post(f"/api/v1/case-types/{ct_id}/restore")
        assert resp.status_code == 409

    async def test_hard_delete(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"HardDel-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-process",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        ct_id = ct.json()["id"]

        resp = await client.delete(f"/api/v1/case-types/{ct_id}?hard=true")
        assert resp.status_code == 204

        get_resp = await client.get(f"/api/v1/case-types/{ct_id}")
        assert get_resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════
# ISO 8601 Duration Parsing (existing, verify still works)
# ═══════════════════════════════════════════════════════════════════

class TestDurationParsing:
    def test_parse_hours(self):
        from case_service.core.sla_tracker import parse_iso8601_duration
        td = parse_iso8601_duration("PT4H")
        assert td == timedelta(hours=4)

    def test_parse_days_hours(self):
        from case_service.core.sla_tracker import parse_iso8601_duration
        td = parse_iso8601_duration("P2DT6H30M")
        assert td == timedelta(days=2, hours=6, minutes=30)

    def test_parse_seconds(self):
        from case_service.core.sla_tracker import parse_iso8601_duration
        td = parse_iso8601_duration("PT30S")
        assert td == timedelta(seconds=30)
