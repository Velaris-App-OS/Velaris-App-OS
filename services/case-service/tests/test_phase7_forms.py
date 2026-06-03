"""Phase 7 tests — Form Builder + Runtime Form Rendering.
Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest
from case_service.db import repository as repo


# ─── Form CRUD Tests ─────────────────────────────────────────────

class TestFormCRUD:
    async def test_create_form(self, client):
        resp = await client.post("/api/v1/forms", json={
            "name": "Simple Form", "version": "1.0.0",
            "definition_json": {"sections": [{"id": "s1", "title": "Main", "order": 0, "fields": []}]},
        })
        assert resp.status_code == 201
        assert resp.json()["name"] == "Simple Form"

    async def test_list_forms(self, client):
        await client.post("/api/v1/forms", json={
            "name": "List Test", "version": "1.0.0", "definition_json": {"sections": []},
        })
        resp = await client.get("/api/v1/forms")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    async def test_get_form(self, client):
        create = await client.post("/api/v1/forms", json={
            "name": "Get Test", "version": "1.0.0",
            "definition_json": {"sections": [{"id": "s1", "title": "Main", "order": 0, "fields": [
                {"id": "f1", "type": "text", "label": "Name", "field_key": "name", "required": True}
            ]}]},
        })
        resp = await client.get(f"/api/v1/forms/{create.json()['id']}")
        assert resp.status_code == 200
        assert len(resp.json()["definition_json"]["sections"][0]["fields"]) == 1

    async def test_update_form(self, client):
        create = await client.post("/api/v1/forms", json={
            "name": "Update Test", "version": "1.0.0", "definition_json": {"sections": []},
        })
        resp = await client.patch(f"/api/v1/forms/{create.json()['id']}", json={
            "definition_json": {"sections": [{"id": "s1", "title": "Updated", "order": 0, "fields": []}]},
        })
        assert resp.status_code == 200
        assert resp.json()["definition_json"]["sections"][0]["title"] == "Updated"

    async def test_delete_form(self, client):
        create = await client.post("/api/v1/forms", json={
            "name": "Delete Test", "version": "1.0.0", "definition_json": {"sections": []},
        })
        fid = create.json()["id"]
        assert (await client.delete(f"/api/v1/forms/{fid}")).status_code == 204
        assert (await client.get(f"/api/v1/forms/{fid}")).status_code == 404


# ─── Form Validation Tests ───────────────────────────────────────

class TestFormValidation:
    def test_validate_required_fields(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "text", "label": "Name", "field_key": "name", "required": True}]}]}
        assert len(_validate_form_values(d, {})) == 1

    def test_validate_text_length(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "text", "label": "Code", "field_key": "code",
             "required": False, "validation": {"min_length": 3, "max_length": 5}}]}]}
        assert len(_validate_form_values(d, {"code": "ab"})) == 1
        assert len(_validate_form_values(d, {"code": "abcdef"})) == 1
        assert len(_validate_form_values(d, {"code": "abc"})) == 0

    def test_validate_number_range(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "number", "label": "Age", "field_key": "age",
             "required": False, "validation": {"min": 0, "max": 150}}]}]}
        assert len(_validate_form_values(d, {"age": -1})) == 1
        assert len(_validate_form_values(d, {"age": 200})) == 1
        assert len(_validate_form_values(d, {"age": 25})) == 0

    def test_validate_email(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "email", "label": "Email", "field_key": "email", "required": False}]}]}
        assert len(_validate_form_values(d, {"email": "bad"})) == 1
        assert len(_validate_form_values(d, {"email": "a@b.com"})) == 0

    def test_validate_dropdown(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "dropdown", "label": "D", "field_key": "d", "required": False,
             "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]}]}]}
        assert len(_validate_form_values(d, {"d": "c"})) == 1
        assert len(_validate_form_values(d, {"d": "a"})) == 0

    def test_validate_all_valid(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "text", "label": "N", "field_key": "n", "required": True},
            {"id": "f2", "type": "email", "label": "E", "field_key": "e", "required": True},
            {"id": "f3", "type": "number", "label": "A", "field_key": "a", "required": False, "validation": {"min": 0}}]}]}
        assert len(_validate_form_values(d, {"n": "John", "e": "j@e.com", "a": 30})) == 0

    def test_number_not_a_number(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "number", "label": "X", "field_key": "x", "required": False}]}]}
        assert len(_validate_form_values(d, {"x": "abc"})) == 1

    def test_empty_form_valid(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "text", "label": "N", "field_key": "n", "required": False}]}]}
        assert len(_validate_form_values(d, {})) == 0

    def test_multiple_required_missing(self):
        from case_service.api.routers.form_submissions import _validate_form_values
        d = {"sections": [{"id": "s1", "title": "M", "order": 0, "fields": [
            {"id": "f1", "type": "text", "label": "A", "field_key": "a", "required": True},
            {"id": "f2", "type": "text", "label": "B", "field_key": "b", "required": True},
            {"id": "f3", "type": "email", "label": "C", "field_key": "c", "required": True}]}]}
        assert len(_validate_form_values(d, {})) == 3


# ─── Form Submission API Tests ───────────────────────────────────

class TestFormSubmissionAPI:
    async def _create_form(self, client):
        resp = await client.post("/api/v1/forms", json={
            "name": "Intake Form", "version": "1.0.0",
            "definition_json": {"sections": [{"id": "s1", "title": "Info", "order": 0, "fields": [
                {"id": "f1", "type": "text", "label": "Full Name", "field_key": "full_name", "required": True, "validation": {"min_length": 2}},
                {"id": "f2", "type": "email", "label": "Email", "field_key": "email", "required": True},
                {"id": "f3", "type": "dropdown", "label": "Dept", "field_key": "department", "required": True,
                 "options": [{"label": "Eng", "value": "engineering"}, {"label": "Sales", "value": "sales"}]},
            ]}]},
        })
        assert resp.status_code == 201
        return resp.json()

    async def _create_case_and_assignment(self, client, session, form_id):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"FormTest-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-process",
            "definition_json": {"stages": [{"id": "stage-1", "name": "Intake", "stage_type": "linear", "order": 0,
                "steps": [{"id": "step-form", "name": "Fill Form", "step_type": "user_task",
                    "bpmn_element_id": "task_1", "required": True, "form_id": form_id,
                    "assignment": {"strategy": "queue_based"}}]}], "sla_policies": []},
        })
        assert ct.status_code == 201
        case = await client.post("/api/v1/cases", json={"case_type_id": ct.json()["id"], "data": {}})
        assert case.status_code == 201

        assignment = await repo.create_assignment(session, data={
            "case_id": uuid.UUID(case.json()["id"]),
            "step_id": "step-form", "assignee_type": "user",
            "assignee_id": "test-user", "status": "active",
        })
        await session.commit()
        return case.json(), str(assignment.id)

    async def test_submit_form_success(self, client, session):
        form = await self._create_form(client)
        case_data, aid = await self._create_case_and_assignment(client, session, form["id"])
        resp = await client.post(f"/api/v1/form-submissions/{aid}", json={
            "form_id": form["id"],
            "values": {"full_name": "Jane Doe", "email": "jane@example.com", "department": "engineering"},
            "completed_by": "test-user",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        case_resp = await client.get(f"/api/v1/cases/{case_data['id']}")
        assert case_resp.json()["data"]["full_name"] == "Jane Doe"

    async def test_submit_form_validation_error(self, client, session):
        form = await self._create_form(client)
        case_data, aid = await self._create_case_and_assignment(client, session, form["id"])
        resp = await client.post(f"/api/v1/form-submissions/{aid}", json={
            "form_id": form["id"], "values": {"email": "notanemail"},
        })
        assert resp.status_code == 422

    async def test_submit_form_assignment_not_found(self, client):
        form = await self._create_form(client)
        resp = await client.post(f"/api/v1/form-submissions/{uuid.uuid4()}", json={
            "form_id": form["id"], "values": {"full_name": "Test"},
        })
        assert resp.status_code == 404

    async def test_submit_form_form_not_found(self, client, session):
        form = await self._create_form(client)
        case_data, aid = await self._create_case_and_assignment(client, session, form["id"])
        resp = await client.post(f"/api/v1/form-submissions/{aid}", json={
            "form_id": str(uuid.uuid4()), "values": {"full_name": "Test"},
        })
        assert resp.status_code == 404

    async def test_get_assignment_form(self, client, session):
        form = await self._create_form(client)
        case_data, aid = await self._create_case_and_assignment(client, session, form["id"])
        resp = await client.get(f"/api/v1/form-submissions/{aid}/form")
        assert resp.status_code == 200
        assert resp.json()["has_form"] is True
        assert resp.json()["form"]["name"] == "Intake Form"

    async def test_get_assignment_form_no_form(self, client, session):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"NoForm-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-process",
            "definition_json": {"stages": [{"id": "s1", "name": "S1", "stage_type": "linear", "order": 0,
                "steps": [{"id": "step-nf", "name": "Manual", "step_type": "user_task",
                    "bpmn_element_id": "t1", "required": True, "assignment": {"strategy": "queue_based"}}]}],
                "sla_policies": []},
        })
        case = await client.post("/api/v1/cases", json={"case_type_id": ct.json()["id"], "data": {}})
        a = await repo.create_assignment(session, data={
            "case_id": uuid.UUID(case.json()["id"]),
            "step_id": "step-nf", "assignee_type": "user", "assignee_id": "u1", "status": "active",
        })
        await session.commit()
        resp = await client.get(f"/api/v1/form-submissions/{a.id}/form")
        assert resp.status_code == 200
        assert resp.json()["has_form"] is False

    async def test_cannot_submit_completed_assignment(self, client, session):
        form = await self._create_form(client)
        case_data, aid = await self._create_case_and_assignment(client, session, form["id"])
        r1 = await client.post(f"/api/v1/form-submissions/{aid}", json={
            "form_id": form["id"],
            "values": {"full_name": "Jane", "email": "j@e.com", "department": "engineering"},
        })
        assert r1.status_code == 200
        r2 = await client.post(f"/api/v1/form-submissions/{aid}", json={
            "form_id": form["id"],
            "values": {"full_name": "Jane2", "email": "j2@e.com", "department": "sales"},
        })
        assert r2.status_code == 409
