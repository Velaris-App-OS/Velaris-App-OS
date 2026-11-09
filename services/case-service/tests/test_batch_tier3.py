"""Tier-3 batch: LLMOps (#32), ai_action (#33), CRDT (#38), manifest v2 (#29),
error codes (catalog wiring).

Each covers the behaviour that is easy to get wrong and invisible when it is:
metrics that silently drop, an AI write that lands unattended, an offline merge
that loses an edit, a permission that can never be satisfied, and an error code
that deep-links to nothing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


# ─── #32 LLMOps ───────────────────────────────────────────────────

class TestLLMOps:
    async def test_cost_is_zero_but_flagged_for_unknown_models(self):
        from velaris_shared.llm import llmops

        assert llmops.estimate_cost_usd("llama3.2", 1000, 500) == (0.0, True)
        cost, known = llmops.estimate_cost_usd("some-new-model", 1000, 500)
        assert (cost, known) == (0.0, False)   # silent zero would hide spend

    async def test_token_estimate_never_zero_for_real_text(self):
        from velaris_shared.llm import llmops

        assert llmops.estimate_tokens_from_chars(0) == 0
        assert llmops.estimate_tokens_from_chars(1) == 1
        assert llmops.estimate_tokens_from_chars(400) == 100

    async def test_queue_never_raises(self):
        """A metrics failure must not turn a working completion into a failure."""
        from velaris_shared.llm import llmops

        llmops.queue_call(
            provider="x", model="y", route="direct", purpose="p",
            prompt_chars=object(),          # type: ignore[arg-type]
            completion_chars=10, latency_ms=1.0,
        )   # must not raise

    async def test_flush_writes_rows_and_metrics_aggregate(self, session):
        from velaris_shared.llm import llmops

        llmops._PENDING.clear()
        for _ in range(3):
            llmops.queue_call(
                provider="ollama", model="llama3.2", route="local",
                purpose="completion", prompt_chars=400, completion_chars=200,
                latency_ms=12.5,
            )
        llmops.queue_call(
            provider="ollama", model="llama3.2", route="local",
            purpose="completion", prompt_chars=400, completion_chars=0,
            latency_ms=5.0, status="error", error_type="TimeoutError",
        )
        assert await llmops.flush_pending(session) == 4
        await session.commit()

        out = await llmops.metrics(session, group_by="model")
        assert out["total_calls"] == 4
        assert out["error_calls"] == 1
        assert out["error_rate"] == 0.25
        assert out["tokens_estimated"] is True

    async def test_bad_group_by_is_rejected(self, session):
        from velaris_shared.llm import llmops

        with pytest.raises(ValueError, match="group_by"):
            await llmops.metrics(session, group_by="; DROP TABLE")


# ─── #38 CRDT ─────────────────────────────────────────────────────

class TestCRDT:
    async def test_higher_lamport_wins(self):
        from case_service.core import crdt

        _v, _s, out = crdt.merge("old", crdt.Stamp(1, "a"), "new", crdt.Stamp(2, "a"))
        assert out == "applied"

    async def test_stale_write_is_ignored_not_applied(self):
        from case_service.core import crdt

        value, _s, out = crdt.merge("current", crdt.Stamp(5, "a"), "stale", crdt.Stamp(2, "b"))
        assert (value, out) == ("current", "ignored")

    async def test_replay_is_idempotent(self):
        from case_service.core import crdt

        stamp = crdt.Stamp(3, "node-a")
        value, _s, out = crdt.merge("v", stamp, "v", stamp)
        assert (value, out) == ("v", "duplicate")

    async def test_equal_lamport_breaks_on_origin_deterministically(self):
        """Two offline clients, same counter — every replica must agree."""
        from case_service.core import crdt

        a, b = crdt.Stamp(4, "aaa"), crdt.Stamp(4, "bbb")
        assert b.dominates(a) and not a.dominates(b)

    async def test_convergence_is_order_independent(self):
        """The property that makes it a CRDT rather than a race."""
        from case_service.core import crdt

        edits = [
            {"key": "k", "value": "A", "lamport": 2, "origin": "a"},
            {"key": "k", "value": "B", "lamport": 5, "origin": "b"},
            {"key": "k", "value": "C", "lamport": 3, "origin": "c"},
        ]
        forward, _ = crdt.merge_all({}, edits)
        reverse, _ = crdt.merge_all({}, list(reversed(edits)))
        assert forward["k"][0] == reverse["k"][0] == "B"

    async def test_origin_is_bounded(self):
        """A hostile client must not win every tie with a huge origin string."""
        from case_service.core import crdt

        assert len(crdt.normalise_origin("z" * 5000)) == crdt.MAX_ORIGIN_LEN

    async def test_next_lamport_follows_the_rule(self):
        from case_service.core import crdt

        assert crdt.next_lamport(3, 7) == 8
        assert crdt.next_lamport(9, 2) == 10

    async def test_negative_lamport_rejected(self):
        from case_service.core import crdt

        with pytest.raises(ValueError):
            crdt.Stamp(-1, "a")

    async def test_sync_rejects_unknown_variable(self, session):
        """Sync converges edits; it does not create new variables."""
        from case_service.core import crdt

        out = await crdt.sync_case_variables(
            session, uuid.uuid4(),
            [{"key": "ns.nope", "value": 1, "lamport": 5, "origin": "a"}],
        )
        assert out["applied"] == 0
        assert out["results"][0]["outcome"] == "rejected"


# ─── #29 manifest v2 ──────────────────────────────────────────────

class TestManifestV2:
    async def test_v1_still_installs(self):
        from velaris_shared.apps import manifest_version as mv

        assert mv.check_manifest_version({}) == 1          # grandfathered
        assert mv.check_manifest_version({"manifest_version": 1}) == 1

    async def test_v2_is_now_supported(self):
        from velaris_shared.apps import manifest_version as mv

        assert mv.check_manifest_version({"manifest_version": 2}) == 2

    async def test_unsupported_version_still_fails_loudly(self):
        from velaris_shared.apps import manifest_version as mv

        with pytest.raises(mv.ManifestVersionError, match="supports manifest_version"):
            mv.check_manifest_version({"manifest_version": 99})

    async def test_v2_rejects_a_relation_the_pdp_cannot_evaluate(self):
        """A permission that is silently never satisfied is worse than a refusal."""
        from velaris_shared.apps import manifest_version as mv

        with pytest.raises(mv.ManifestVersionError, match="relation"):
            mv.validate_v2_permissions({
                "permissions": [{"relation": "superuser", "object_type": "case"}]
            })

    async def test_v2_rejects_unknown_object_type(self):
        from velaris_shared.apps import manifest_version as mv

        with pytest.raises(mv.ManifestVersionError, match="object_type"):
            mv.validate_v2_permissions({
                "permissions": [{"relation": "viewer", "object_type": "database"}]
            })

    async def test_v1_roles_translate_without_rewriting_the_package(self):
        from velaris_shared.apps import manifest_version as mv

        out = mv.effective_permissions({"required_roles": ["case_worker", "viewer"]})
        assert {p["relation"] for p in out} == {"assignee", "viewer"}

    async def test_unknown_v1_role_degrades_to_least_privilege(self):
        """Guessing upward on an unrecognised role would widen reach silently."""
        from velaris_shared.apps import manifest_version as mv

        out = mv.effective_permissions({"required_roles": ["wizard"]})
        assert out == [{"relation": "viewer", "object_type": "case", "subject_type": "user"}]

    async def test_platform_still_writes_v1(self):
        from velaris_shared.apps import manifest_version as mv

        assert mv.MANIFEST_VERSION == 1


# ─── Error catalog wiring ─────────────────────────────────────────

class TestErrorCodes:
    async def test_undocumented_code_is_refused(self):
        """A code deep-linking to a page that documents nothing is worse than none."""
        from velaris_shared.core import error_codes as ec

        with pytest.raises(ValueError, match="not a documented error code"):
            ec.coded(500, "VEL-MADE-UP-500", "boom")

    async def test_coded_error_carries_code_and_status(self):
        from velaris_shared.core import error_codes as ec

        exc = ec.coded(503, ec.DOC_STORAGE_503, "storage down")
        assert exc.code == ec.DOC_STORAGE_503
        assert exc.status_code == 503
        assert exc.detail == "storage down"

    async def test_is_still_an_httpexception(self):
        """Additive by design — existing handlers and tests must keep working."""
        from fastapi import HTTPException

        from velaris_shared.core import error_codes as ec

        assert isinstance(ec.coded(404, ec.DOC_NOTFOUND_404, "x"), HTTPException)

    async def test_payload_shape_keeps_detail_where_it_was(self):
        from velaris_shared.core import error_codes as ec

        body = ec.error_payload(ec.DOC_UUID_422, "bad uuid")
        assert body["detail"] == "bad uuid"
        assert body["error"]["code"] == ec.DOC_UUID_422
        assert ec.DOC_UUID_422 in body["error"]["docs"]

    async def test_every_known_code_is_in_the_catalog(self):
        """The registry and the document must not drift apart."""
        import pathlib
        import re

        from velaris_shared.core import error_codes as ec

        catalog = pathlib.Path(__file__).resolve().parents[3] / "docs/Future/error-catalog.md"
        if not catalog.exists():          # docs/ is not shipped in the mirror
            pytest.skip("error-catalog.md not present in this tree")
        documented = set(re.findall(r"VEL-[A-Z]+-[A-Z0-9-]+-\d{3}", catalog.read_text()))
        assert ec.KNOWN_CODES <= documented


# ─── Security regressions (found auditing the above, 2026-08-03) ──

class TestSecurityRegressions:
    """Each of these was a real hole in the code written above.

    They are pinned as tests rather than just fixed, because every one of them
    was invisible from the outside: the endpoints worked perfectly for a
    legitimate caller, which is exactly why they got past the first pass.
    """

    async def test_llmops_metrics_requires_authentication(self, anon_client):
        """It shipped with only `get_session` — cost and model data, unauthenticated."""
        resp = await anon_client.get("/api/v1/hxnexus/llmops/metrics")
        assert resp.status_code in (401, 403), resp.text

    async def test_llmops_metrics_route_declares_a_role_check(self):
        from case_service.main import app

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/hxnexus/llmops/metrics")
        names = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
        # A session plus at least one more (the role check). The session is the
        # routed read session since R5d 11b.
        assert ({"get_session", "get_read_session"} & set(names)) and len(names) > 1, names

    async def test_case_routes_declare_authentication(self):
        """Every route added in this batch must at least authenticate."""
        from case_service.main import app

        for path in (
            "/api/v1/cases/{case_id}/variables/sync",
            "/api/v1/cases/{case_id}/ai-actions",
            "/api/v1/cases/{case_id}/ai-actions/{proposal_id}/decide",
        ):
            route = next(r for r in app.routes if getattr(r, "path", "") == path)
            names = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
            assert "get_current_user" in names, (path, names)

    async def test_case_routes_enforce_relationship_access(self):
        """Authentication is not authorization — these must call hxguard."""
        import inspect

        from case_service.api.routers import cases as cases_router

        for fn, action in (
            (cases_router.sync_offline_variable_edits, "case.update"),
            (cases_router.list_ai_action_proposals, "case.read"),
            (cases_router.decide_ai_action_proposal, "case.update"),
        ):
            src = inspect.getsource(fn)
            assert "hxguard.require_case" in src, fn.__name__
            assert action in src, (fn.__name__, action)

    async def test_crdt_sync_reenforces_the_value_cap(self, session):
        """Writing rows directly skips the façade, so the 64KB cap had to be redone."""
        from case_service.core import crdt
        from velaris_shared.db.models import (
            CaseInstanceModel, CaseInstanceVariableModel, CaseTypeModel,
        )

        ct = CaseTypeModel(name=f"CapCT {uuid.uuid4().hex[:6]}", version="1.0.0",
                           definition_json={"stages": []})
        session.add(ct)
        await session.flush()
        case = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                                 status="open")
        session.add(case)
        await session.flush()
        session.add(CaseInstanceVariableModel(
            case_id=case.id, full_key="ns.big", value_text="small",
            written_by="test", lamport=1, origin="seed",
        ))
        await session.flush()

        out = await crdt.sync_case_variables(
            session, case.id,
            [{"key": "ns.big", "value": "x" * 70_000, "lamport": 9, "origin": "a"}],
        )
        assert out["applied"] == 0
        assert "64KB" in out["results"][0]["reason"]

    async def test_crdt_sync_applies_a_legitimate_edit(self, session):
        """The cap must not have broken the normal path."""
        from case_service.core import crdt
        from velaris_shared.db.models import (
            CaseInstanceModel, CaseInstanceVariableModel, CaseTypeModel,
        )

        ct = CaseTypeModel(name=f"OkCT {uuid.uuid4().hex[:6]}", version="1.0.0",
                           definition_json={"stages": []})
        session.add(ct)
        await session.flush()
        case = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                                 status="open")
        session.add(case)
        await session.flush()
        session.add(CaseInstanceVariableModel(
            case_id=case.id, full_key="ns.k", value_text="old",
            written_by="test", lamport=1, origin="seed",
        ))
        await session.flush()

        out = await crdt.sync_case_variables(
            session, case.id,
            [{"key": "ns.k", "value": "new", "lamport": 5, "origin": "phone"}],
        )
        assert out["applied"] == 1
        assert out["results"][0]["outcome"] == "applied"

    async def test_proposal_listing_is_bounded(self):
        from case_service.core import ai_action

        assert ai_action.MAX_PROPOSALS_RETURNED <= 200

    async def test_ai_action_context_is_redacted_before_reaching_a_model(self):
        """Case context goes into a prompt — secrets must not ride along."""
        from case_service.case_vars.service import CallerContext, _redact_value

        ctx = CallerContext(kind="platform", actor_id="ai-action")
        assert ctx.privileged is False
        assert _redact_value("s3cret", "secret", ctx, is_owner=False) == "***"
        assert _redact_value("bob@x.com", "pii", ctx, is_owner=False) == "***"

    async def test_ai_action_writes_are_grant_checked_not_privileged(self):
        """It must use the grant-checked path, never a platform bypass."""
        import inspect

        from case_service.core import ai_action

        src = inspect.getsource(ai_action._apply)
        assert "set_granted" in src
        assert "set_variable" not in src


class TestAIActionEndToEnd:
    """#33 could not write at all until the `ai` namespace and grant existed.

    The grant-checked path was the right design and also the reason the step
    type was inert: no namespace, no grantee, every accepted proposal failed
    closed. Migration 147 seeds both.
    """

    async def _seed_namespace(self, session):
        from velaris_shared.db.models import NamespaceGrantModel, VariableNamespaceModel
        from sqlalchemy import select

        ns = (await session.execute(
            select(VariableNamespaceModel).where(VariableNamespaceModel.name == "ai")
        )).scalar_one_or_none()
        if ns is None:
            ns = VariableNamespaceModel(
                name="ai", owner_type="platform", owner_ref=None,
                sensitivity="internal", status="active",
            )
            session.add(ns)
            await session.flush()
        grant = (await session.execute(
            select(NamespaceGrantModel).where(
                NamespaceGrantModel.namespace_id == ns.id,
                NamespaceGrantModel.grantee_type == "ai_action",
            )
        )).scalar_one_or_none()
        if grant is None:
            session.add(NamespaceGrantModel(
                namespace_id=ns.id, grantee_type="ai_action", grantee_ref="*",
                capability="write", granted_by="test",
            ))
            await session.flush()
        return ns

    async def _case(self, session):
        """Built on the test's own connection.

        Creating it through `client` while this session holds an uncommitted
        flush deadlocks SQLite — the documented flush -> client-call -> commit
        anti-pattern in this suite.
        """
        from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel

        ct = CaseTypeModel(name=f"AIA {uuid.uuid4().hex[:6]}", version="1.0.0",
                           definition_json={"stages": []})
        session.add(ct)
        await session.flush()
        c = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                              status="open")
        session.add(c)
        await session.flush()
        return c.id

    async def test_accepted_proposal_writes_the_variable(self, session):
        """The whole point of the step type — an accepted answer lands."""
        from case_service.case_vars import service as case_vars
        from case_service.core import ai_action
        from velaris_shared.db.models import AIActionProposalModel

        await self._seed_namespace(session)
        case_id = await self._case(session)

        proposal = AIActionProposalModel(
            case_id=case_id, step_id="s1", status="pending",
            proposed={"value": "high", "reasoning": "because"},
        )
        session.add(proposal)
        await session.flush()

        step = {"id": "s1", "ai_action_config": {
            "prompt": "p", "output_variable": "band", "namespace": "ai",
        }}
        out = await ai_action.decide(
            session, proposal_id=proposal.id, decision="accept",
            actor_id="reviewer", step=step,
        )
        assert out["status"] == "accepted"

        ctx = case_vars.CallerContext(kind="operator", actor_id="reviewer",
                                      privileged=True)
        values = await case_vars.get_all(session, ctx, case_id)
        assert values.get("ai.band") == "high"

    async def test_override_writes_the_humans_value_not_the_models(self, session):
        from case_service.case_vars import service as case_vars
        from case_service.core import ai_action
        from velaris_shared.db.models import AIActionProposalModel

        await self._seed_namespace(session)
        case_id = await self._case(session)

        proposal = AIActionProposalModel(
            case_id=case_id, step_id="s1", status="pending",
            proposed={"value": "model-said", "reasoning": "r"},
        )
        session.add(proposal)
        await session.flush()

        step = {"id": "s1", "ai_action_config": {
            "prompt": "p", "output_variable": "band", "namespace": "ai",
        }}
        out = await ai_action.decide(
            session, proposal_id=proposal.id, decision="override",
            actor_id="reviewer", step=step, override_value="human-said",
        )
        assert out["status"] == "overridden"

        ctx = case_vars.CallerContext(kind="operator", actor_id="reviewer",
                                      privileged=True)
        values = await case_vars.get_all(session, ctx, case_id)
        assert values.get("ai.band") == "human-said"
        # Both sides are retained — that is what makes accuracy measurable.
        await session.refresh(proposal)
        assert proposal.proposed["value"] == "model-said"
        assert proposal.final_output["value"] == "human-said"


class TestSecondSecurityPass:
    """Holes found on the second audit of this batch (2026-08-03).

    Both were the same shape as the first pass: the feature worked perfectly
    for a legitimate caller, and leaked or misled for everyone else.
    """

    async def test_migration_report_requires_designer_or_admin(self):
        """It enumerates case-type and rule NAMES — not general-audience data."""
        import inspect

        from case_service.api.routers import rules as rules_router

        src = inspect.getsource(rules_router.rule_migration_report)
        assert "designer" in src and "403" in src

    async def test_migration_report_is_tenant_scoped(self):
        """Unscoped, it exposed every tenant's process inventory to any user."""
        import inspect

        from case_service.api.routers import rules as rules_router

        src = inspect.getsource(rules_router.rule_migration_report)
        assert "user.tenant_id" in src
        assert "CaseTypeModel.tenant_id" in src

    async def test_encryption_failure_is_matched_on_type_not_message(self):
        """A message sniff for 'key' would report any KeyError as an
        encryption fault and send the operator to the wrong fix."""
        import inspect

        from case_service.api.routers import documents as docs_router

        src = inspect.getsource(docs_router)
        assert "except StorageError" in src
        assert '"key" in str(e).lower()' not in src

    async def test_every_new_case_route_enforces_relationship_access(self):
        import inspect

        from case_service.api.routers import cases as cases_router

        for name in (
            "get_interpreter_state", "get_case_event_stream",
            "verify_case_event_stream", "sync_offline_variable_edits",
            "list_ai_action_proposals", "decide_ai_action_proposal",
        ):
            src = inspect.getsource(getattr(cases_router, name))
            assert "hxguard.require_case" in src, name
