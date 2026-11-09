"""The gaps found on 2026-08-29 by reading HxArbiter for what it did NOT do.

Five things the engine claimed, promised or implied and did not deliver:

* **Obligations never executed** — covered in `test_phaseARB_peps_e2e.py`.
* **The decision log was never anchored.** `hxarbiter/__init__.py` said "one
  anchored decision log" since ARB-A; `audit_anchors` held receipts over the
  CASE audit chain only. Hash-chaining is tamper-evident against a writer, not
  against someone who can rewrite the whole chain — which is the party a
  sovereignty product is asked about.
* **No retention on `arbiter_decisions`.** Nothing in `compliance/retention.py`
  touched it, so the evidence surface grew without bound and no erasure request
  could reach it.
* **No way to move a rule set.** No export, no import: no backup short of a
  database dump, and no path from staging to production.
* **The retirement soak was designed and not built.** §19.6 has a retired rule
  keep evaluating so an operator can see what it would still have caught;
  `load_active_policies` filtered retired policies out and that was that.

The soak tests are the sharpest here, because the *obvious* implementation is
wrong in a way that looks safe — see
`test_a_soaking_policy_cannot_change_the_live_decision`.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    ArbiterDecisionModel,
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    SecurityEventModel,
    TenantModel,
)

from case_service.hxarbiter import (
    PolicyCompiler,
    PolicyMode,
    Rule,
    VELARIS_CATALOG,
)
from case_service.hxarbiter import chain, pdp, pep, store

pytestmark = pytest.mark.asyncio

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow")]


@pytest_asyncio.fixture(autouse=True)
async def tenant(session):
    from case_service.tenancy import identity as _identity
    _identity.clear_cache()
    pdp.invalidate_cache()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    pdp.invalidate_cache()
    _identity.clear_cache()


def _rule(rid, effect, condition):
    return Rule.model_validate({"id": rid, "effect": effect, "condition": condition})


async def _activate(session, tenant, name, rules, *, namespace="ai",
                    mode=PolicyMode.SHADOW):
    p = await store.create_policy(session, tenant_id=tenant.id, name=name,
                                  namespace=namespace)
    await store.add_version(session, policy_id=p.id, rules=rules,
                            compiler=PolicyCompiler(VELARIS_CATALOG), mode=mode)
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()
    pdp.invalidate_cache()
    return p


DENY_EXTERNAL = {"attr": "context.provider_class", "op": "eq", "value": "external"}


PREFIX = "/api/v1/arbiter"

DENY_EXTERNAL_RULE = {
    "id": "r1", "effect": "deny",
    "condition": {"attr": "context.provider_class", "op": "eq",
                  "value": "external"},
}


def _tenant_admin_headers() -> dict[str, str]:
    """An `admin` who is NOT a superadmin — on a shared install that is what
    "admin" means, and platform scope reaches every tenant on the box."""
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="arb-tenant-admin", roles=["admin"],
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def _make_policy(client, name: str | None = None) -> str:
    body = {"name": name or f"p-{uuid.uuid4().hex[:8]}", "namespace": "ai"}
    r = await client.post(f"{PREFIX}/policies", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ══ the retirement soak (§19.6) ═════════════════════════════════════════════


async def test_a_retired_policy_is_still_observed_within_the_soak(
        session, tenant):
    p = await _activate(session, tenant, "soon-retired",
                        [_rule("r1", "deny", DENY_EXTERNAL)])
    await store.retire_policy(session, policy_id=p.id)
    await session.commit()
    pdp.invalidate_cache()

    await pep.check(session, tenant_id="default", action="ai.external_call",
                    subject_id="u", context={"provider_class": "external"},
                    legacy_allowed=True)
    await session.commit()

    ev = (await session.execute(
        select(SecurityEventModel)
        .where(SecurityEventModel.event_type == "arbiter_retirement_soak")
    )).scalars().first()
    assert ev is not None, "the retired rule's soak was never observed"
    assert ev.details["would_have"] == "deny"
    assert ev.details["matched"]


async def test_a_soaking_policy_cannot_change_the_live_decision(session, tenant):
    """The reason the soak is a SEPARATE evaluation.

    The obvious implementation — let the retired policy back into the live set
    at `shadow` — looks safe because the clamp stops it binding. It is not:
    under deny-overrides a retired **deny** changes the decision's `effect`, so
    the answer in the log stops being the answer the platform gave; it appears
    in `matched`, so the audit trail names a rule that was not in force; and it
    reads as `engine_stricter` in the parity report, which can block a
    promotion that was eligible the day before the upgrade.
    """
    p = await _activate(session, tenant, "retired-deny",
                        [_rule("r1", "deny", DENY_EXTERNAL)])
    await store.retire_policy(session, policy_id=p.id)
    # An allow that IS live, so the namespace default cannot mask the point.
    await _activate(session, tenant, "live-allow",
                    [_rule("a1", "allow", DENY_EXTERNAL)])

    decision = await pep.evaluate(
        session, tenant_id="default", action="ai.external_call",
        subject_id="u", context={"provider_class": "external"},
        legacy_allowed=True)
    await session.commit()

    assert str(decision.effect) == "allow", (
        "a retired deny changed the live answer — the soak leaked into the "
        "decision")
    assert all(str(m.policy_id) != str(p.id) for m in decision.matched), (
        "a retired policy appeared in `matched`, so the audit trail names a "
        "rule that was not in force")


async def test_a_policy_retired_longer_ago_than_the_soak_is_not_observed(
        session, tenant):
    p = await _activate(session, tenant, "long-gone",
                        [_rule("r1", "deny", DENY_EXTERNAL)])
    await store.retire_policy(session, policy_id=p.id)
    row = await session.get(ArbiterPolicyModel, p.id)
    row.retired_at = datetime.now(timezone.utc) - timedelta(
        days=store.RETIREMENT_SOAK_DAYS + 2)
    await session.commit()
    pdp.invalidate_cache()

    await pep.check(session, tenant_id="default", action="ai.external_call",
                    subject_id="u", context={"provider_class": "external"},
                    legacy_allowed=True)
    await session.commit()
    assert (await session.execute(
        select(SecurityEventModel)
        .where(SecurityEventModel.event_type == "arbiter_retirement_soak")
    )).scalars().first() is None


async def test_a_soak_that_matches_nothing_records_nothing(session, tenant):
    """Otherwise every request in the namespace would produce a row, and the
    signal an operator is looking for would be buried under it."""
    p = await _activate(session, tenant, "narrow",
                        [_rule("r1", "deny", {"attr": "tenant.region",
                                              "op": "eq", "value": "nowhere"})])
    await store.retire_policy(session, policy_id=p.id)
    await session.commit()
    pdp.invalidate_cache()

    await pep.check(session, tenant_id="default", action="ai.external_call",
                    subject_id="u", context={"provider_class": "external"},
                    legacy_allowed=True)
    await session.commit()
    assert (await session.execute(
        select(SecurityEventModel)
        .where(SecurityEventModel.event_type == "arbiter_retirement_soak")
    )).scalars().first() is None


# ══ retention ═══════════════════════════════════════════════════════════════


async def _decision(session, tenant, *, namespace="ai", age_days=1, sealed=True):
    row = ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace=namespace,
        action=f"{namespace}.x", subject_kind="service", subject_id="s",
        subject={}, resource={}, context={}, effect="deny", mode="shadow",
        enforced=False, default_applied=True, matched=[], obligations=[],
        chain_sequence=1 if sealed else None,
        evaluated_at=datetime.now(timezone.utc) - timedelta(days=age_days))
    session.add(row)
    await session.commit()
    return row


async def test_retention_unset_means_keep_forever(session, tenant):
    """Every namespace ships with it unset, and an upgrade must never start
    deleting an operator's evidence for them."""
    await _decision(session, tenant, age_days=4000)
    assert await chain.purge_expired_decisions(session) == {}


async def test_a_configured_horizon_purges_older_decisions(session, tenant):
    ns = await session.get(ArbiterNamespaceModel, "ai")
    ns.decision_retention_days = 30
    await session.commit()
    await _decision(session, tenant, age_days=90)
    await _decision(session, tenant, age_days=2)

    purged = await chain.purge_expired_decisions(session)
    await session.commit()
    assert purged == {"ai": 1}
    left = (await session.execute(select(ArbiterDecisionModel))).scalars().all()
    assert len(left) == 1


async def test_an_unsealed_decision_is_never_purged(session, tenant):
    """A gap in the sealed windows is itself evidence that rows were removed.
    Deleting a row the chain has not yet covered is indistinguishable from
    tampering, so it waits for the sealer."""
    ns = await session.get(ArbiterNamespaceModel, "ai")
    ns.decision_retention_days = 1
    await session.commit()
    await _decision(session, tenant, age_days=90, sealed=False)

    assert await chain.purge_expired_decisions(session) == {}
    assert (await session.execute(
        select(ArbiterDecisionModel))).scalars().first() is not None


# ══ anchoring ═══════════════════════════════════════════════════════════════


async def test_the_arbiter_chain_has_its_own_anchor_lineage(session, tenant):
    """Receipts over two different chains must not be confused for each other:
    `_latest_anchor` decides whether the tip has moved, and mixing them would
    make one chain's anchor suppress the other's."""
    from case_service.compliance import audit_anchor
    from velaris_shared.db.models import AuditAnchorModel

    session.add(AuditAnchorModel(
        id=uuid.uuid4(), chain=audit_anchor.CHAIN_AUDIT, tip_sequence=1,
        tip_hash="a" * 64, tsa_url="http://tsa.invalid", tsr_der=b"x",
        anchored_at=datetime.now(timezone.utc)))
    await session.commit()

    assert await audit_anchor._latest_anchor(
        session, audit_anchor.CHAIN_ARBITER) is None
    assert await audit_anchor._latest_anchor(
        session, audit_anchor.CHAIN_AUDIT) is not None


async def test_anchoring_an_empty_decision_chain_is_a_no_op_not_a_failure(
        session, tenant):
    """It must not call a TSA — a fresh install has nothing to timestamp, and a
    background loop that errors every cycle on an idle platform trains an
    operator to ignore the log."""
    from case_service.compliance import audit_anchor

    result = await audit_anchor.anchor_chain_tip(
        session, chain=audit_anchor.CHAIN_ARBITER)
    assert result["anchored"] is False
    assert result["reason"] == "empty_chain"
    assert result["chain"] == "arbiter"


# ══ export / import ═════════════════════════════════════════════════════════


async def test_export_carries_rules_but_not_history_or_evidence(client):
    """What a portable document must NOT contain, and why each one matters.

    Activation intervals are facts about the install that ran the policy —
    copying them would put a fabricated enforcement history into the importing
    install's answer to "which rule was live on 3 March". The decision log is
    evidence about one deployment and is hash-chained where it lives.
    """
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions",
                      json={"rules": [DENY_EXTERNAL_RULE]})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")

    doc = (await client.get(f"{PREFIX}/export")).json()
    assert doc["format"] == "hxarbiter-policy-export"
    assert doc["catalog"] and doc["namespaces"]
    entry = [p for p in doc["policies"] if p["rules"]][0]
    assert "activated_at" not in entry and "deactivated_at" not in entry
    assert "decisions" not in doc
    # The ceiling does not travel: it is a statement about the importing
    # deployment's rollout, and a document must not switch enforcement on
    # somewhere else.
    assert all("max_mode" not in n for n in doc["namespaces"])


async def test_an_import_lands_in_shadow_and_is_not_activated(client):
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions",
                      json={"rules": [DENY_EXTERNAL_RULE], "mode": "enforce"})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")
    doc = (await client.get(f"{PREFIX}/export")).json()
    for p in doc["policies"]:
        p["name"] = "imported-" + p["name"]

    r = await client.post(f"{PREFIX}/import", json={"document": doc})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mode"] == "shadow", (
        "a document is somebody else's judgement about somebody else's "
        "platform; it becomes this one's when a person here says so")
    assert body["activated"] is False


async def test_a_duplicate_name_is_refused_by_default(client):
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions",
                      json={"rules": [DENY_EXTERNAL_RULE]})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")
    doc = (await client.get(f"{PREFIX}/export")).json()

    r = await client.post(f"{PREFIX}/import", json={"document": doc})
    assert r.status_code == 422
    assert "already exists" in r.text


async def test_a_document_with_one_bad_policy_imports_NOTHING(client):
    """Two passes on purpose. A partial import leaves a rule set nobody wrote,
    and the operator has no way to tell which half arrived."""
    doc = {
        "format": "hxarbiter-policy-export", "version": 1,
        "policies": [
            {"name": "good-one", "namespace": "ai", "rules": [DENY_EXTERNAL_RULE]},
            {"name": "bad-one", "namespace": "ai",
             "rules": [{"id": "b", "effect": "deny",
                        "condition": {"attr": "tenant.no_such_path",
                                      "op": "eq", "value": "x"}}]},
        ],
    }
    r = await client.post(f"{PREFIX}/import", json={"document": doc})
    assert r.status_code == 422
    assert "bad-one" in r.text
    names = [p["name"] for p in (await client.get(f"{PREFIX}/policies")).json()]
    assert "good-one" not in names, "a partial import was left behind"


async def test_import_refuses_a_document_it_did_not_produce(client):
    assert (await client.post(
        f"{PREFIX}/import",
        json={"document": {"policies": []}})).status_code == 422


async def test_a_tenant_admin_cannot_import_platform_policies(client):
    """Platform scope reaches every tenant on the box, so it is a platform
    operator's act — the same rule as creating one by hand."""
    doc = {"format": "hxarbiter-policy-export", "version": 1,
           "policies": [{"name": "plat", "namespace": "ai",
                         "rules": [DENY_EXTERNAL_RULE]}]}
    r = await client.post(f"{PREFIX}/import",
                          json={"document": doc, "platform": True},
                          headers=_tenant_admin_headers())
    assert r.status_code == 403
    assert "PLATFORM-SCOPE" in r.text


# ══ the adversarial pass, 2026-08-29 ════════════════════════════════════════


async def test_a_caller_cannot_spoof_a_structural_attribute(client):
    """`subject.kind`, `subject.id` and `resource.kind` come from the request's
    own fields, never from the free-form `attrs`/`context` maps a caller also
    controls. Without that precedence, a rule keyed on `subject.kind` could be
    dodged by putting `kind` in `attrs`.

    (`subject.attrs` and `context` ARE caller-supplied by design — §14 says a
    PEP is trusted code and a request body is not, and `/decide` is gated on
    `arbiter.decisions.evaluate`. This pins the part that must not be.)
    """
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions", json={"rules": [
        {"id": "r1", "effect": "deny",
         "condition": {"attr": "subject.kind", "op": "eq", "value": "user"}}]})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")

    async def effect(subject, context):
        r = await client.post(f"{PREFIX}/decide", json={
            "subject": subject, "action": "ai.external_call",
            "resource": {"kind": "case"}, "context": context})
        return r.json()

    honest = await effect({"kind": "user", "id": "u"}, {})
    assert honest["matched"], "the rule did not fire on an honest request"

    for subject, context in (
        ({"kind": "user", "id": "u", "attrs": {"kind": "service"}}, {}),
        ({"kind": "user", "id": "u"}, {"subject.kind": "service"}),
        ({"kind": "user", "id": "u", "attrs": {"id": "someone-else"}}, {}),
    ):
        body = await effect(subject, context)
        assert body["matched"], f"the rule was dodged by {subject} / {context}"


async def test_a_caller_cannot_supply_a_tenant_attribute(client):
    """`tenant.*` is resolved by a PIP through HxGlobal. `RequestResolver` has
    prefix `*` — it is asked for EVERY path — so it must serve only the three
    heads it owns, or a caller could put `tenant.region` in `context` and
    answer the one attribute the product's main claim rests on."""
    from case_service.hxarbiter import pdp
    from hxarbiter_core import RequestResolver, DecisionRequest, SubjectRef, ResourceRef

    req = DecisionRequest(
        tenant_id="default", subject=SubjectRef(kind="user", id="u"),
        action="ai.external_call", resource=ResourceRef(kind="case"),
        context={"tenant.region": "us-east", "region": "us-east"})
    out = await RequestResolver().resolve(req, {"tenant.region", "tenant.plan"})
    assert out == {}, f"a caller answered a tenant attribute: {out}"


async def test_an_unregistered_namespace_is_a_404_not_a_500(client):
    """`arbiter_policies.namespace` is a foreign key, so a typo was caught — by
    the database, as a 500. A typo is ordinary user input; it must read as one,
    and the FK error text differs per dialect."""
    r = await client.post(f"{PREFIX}/policies",
                          json={"name": "bad-ns", "namespace": "no-such-ns"})
    assert r.status_code == 404
    assert "VEL-ARB-NAMESPACE-404" in r.text


async def test_importing_into_an_unregistered_namespace_is_reported_not_500(client):
    doc = {"format": "hxarbiter-policy-export", "version": 1,
           "policies": [{"name": "x", "namespace": "no-such-ns",
                         "rules": [DENY_EXTERNAL_RULE]}]}
    r = await client.post(f"{PREFIX}/import", json={"document": doc})
    assert r.status_code == 422
    assert "not registered" in r.text


async def test_an_imported_document_cannot_raise_a_ceiling_or_a_scope(client):
    """The three escalations a document might try. All ignored: the ceiling is
    a statement about the IMPORTING deployment's rollout, and platform scope
    binds every tenant on the box."""
    doc = {"format": "hxarbiter-policy-export", "version": 1,
           "namespaces": [{"namespace": "ai", "default_effect": "allow",
                           "max_mode": "enforce"}],
           "policies": [{"name": "sneaky", "namespace": "ai",
                         "scope": "platform", "mode": "enforce",
                         "rules": [DENY_EXTERNAL_RULE]}]}
    assert (await client.post(f"{PREFIX}/import",
                              json={"document": doc})).status_code == 201

    listed = [p for p in (await client.get(f"{PREFIX}/policies")).json()
              if p["name"] == "sneaky"][0]
    assert listed["scope"] == "tenant"
    assert listed["active_version"] is None      # not activated

    cat = (await client.get(f"{PREFIX}/catalog")).json()
    ai = [n for n in cat["namespaces"] if n["namespace"] == "ai"][0]
    assert ai["max_mode"] == "shadow"
    assert ai["default_effect"] == "deny"


async def test_a_retention_horizon_is_settable_and_visible(client):
    """The column existed and nothing exposed it: a control configurable only
    by direct SQL is one nobody reviews."""
    r = await client.put(f"{PREFIX}/namespaces/ai/retention",
                         json={"decision_retention_days": 90})
    assert r.status_code == 200
    assert r.json()["decision_retention_days"] == 90

    cat = (await client.get(f"{PREFIX}/catalog")).json()
    ai = [n for n in cat["namespaces"] if n["namespace"] == "ai"][0]
    assert ai["decision_retention_days"] == 90

    # And "keep forever" must remain expressible — not a one-way door.
    assert (await client.put(f"{PREFIX}/namespaces/ai/retention",
                             json={"decision_retention_days": None})
            ).json()["decision_retention_days"] is None


async def test_setting_retention_is_a_platform_operators_act(client):
    r = await client.put(f"{PREFIX}/namespaces/ai/retention",
                         json={"decision_retention_days": 30},
                         headers=_tenant_admin_headers())
    assert r.status_code == 403
    assert "PLATFORM-SCOPE" in r.text


# ══ erasure reaching the decision log ═══════════════════════════════════════


async def test_an_erasure_scrubs_the_decision_payloads(session, tenant):
    """A GDPR erasure could not reach `arbiter_decisions` at all: retention
    purges by age, and `anonymize_user_data` had never heard of the table. Every
    decision about a person kept their subject, resource and context payloads
    indefinitely — in a log designed never to be editable."""
    from case_service.enterprise import gdpr

    session.add(ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", action="ai.x",
        subject_kind="user", subject_id="person-1",
        subject={"id": "person-1", "email": "p@example.com"},
        resource={"kind": "case", "id": "c1"},
        context={"note": "about that person"},
        effect="deny", mode="shadow", enforced=False, default_applied=False,
        matched=[], obligations=[], chain_sequence=1,
        evaluated_at=datetime.now(timezone.utc)))
    await session.commit()

    await gdpr.anonymize_user_data(session, "person-1")
    await session.commit()

    row = (await session.execute(
        select(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.subject_id == "person-1")
    )).scalars().first()
    assert row is not None
    assert row.subject == {}
    assert row.resource == {}
    assert row.context == {"erased": True}


async def test_an_erasure_does_not_break_the_seal(session, tenant):
    """What is scrubbed is decided by the hash, not by taste.

    `compute_batch_hash` covers `subject_id` and does NOT cover `subject`,
    `resource` or `context`. Scrubbing the three blobs erases where the
    personal detail actually lives; changing `subject_id` would break the chain
    from that batch onward, so the log would stop being able to prove it had
    not been edited — a worse outcome for everyone, the data subject included.
    """
    row = ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", action="ai.x",
        subject_kind="user", subject_id="person-2",
        subject={"email": "q@example.com"}, resource={}, context={"x": "y"},
        effect="deny", mode="shadow", enforced=False, default_applied=False,
        matched=[], obligations=[],
        evaluated_at=datetime.now(timezone.utc) - timedelta(minutes=10))
    session.add(row)
    await session.commit()

    await chain.seal_once(session)
    await session.commit()
    ok, problem = await chain.verify_chain(session)
    assert ok, problem

    await chain.erase_subject(session, "person-2")
    await session.commit()

    ok, problem = await chain.verify_chain(session)
    assert ok, f"the erasure broke the chain: {problem}"

    scrubbed = await session.get(ArbiterDecisionModel, row.id)
    assert scrubbed.subject_id == "person-2", (
        "the identifier is retained deliberately — it is covered by the hash")


# ══ what-if replay — the column nothing ever wrote ══════════════════════════


async def test_a_replay_refuses_when_the_log_cannot_supply_the_attributes(
        session, tenant):
    """The most dangerous possible output from this function is a confident
    "nothing would change".

    The decision log stores the request and NOT the resolved attribute map:
    `tenant.region` and `consent.basis` were never written down. Replaying a
    residency rule anyway would treat the region as missing, make every
    comparison false, and report that a rule which would deny half the traffic
    changes nothing. So it refuses and names the paths.
    """
    from case_service.hxarbiter.nlp import whatif

    report = await whatif.replay(
        session,
        rules=[_rule("r", "deny", {"attr": "tenant.region", "op": "eq",
                                   "value": "eu-west"})],
        namespace="ai", tenant_id=tenant.id, catalog=VELARIS_CATALOG)
    assert report["replayed"] is False
    assert report["reason"] == "attributes_not_recorded"
    assert report["unreplayable_attributes"] == ["tenant.region"]


async def test_a_request_only_rule_is_replayed_against_real_decisions(
        session, tenant):
    from case_service.hxarbiter.nlp import whatif

    for effect in ("allow", "allow", "deny"):
        session.add(ArbiterDecisionModel(
            id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai",
            action="ai.external_call", subject_kind="service", subject_id="s",
            subject={}, resource={}, context={"provider_class": "external"},
            effect=effect, mode="shadow", enforced=False,
            default_applied=False, matched=[], obligations=[],
            evaluated_at=datetime.now(timezone.utc)))
    await session.commit()

    report = await whatif.replay(
        session,
        rules=[_rule("r", "deny", {"attr": "context.provider_class",
                                   "op": "eq", "value": "external"})],
        namespace="ai", tenant_id=tenant.id, catalog=VELARIS_CATALOG)
    assert report["replayed"] is True
    assert report["compared"] == 3
    assert report["would_now_deny"] == 2      # the two that were allowed
    assert report["unchanged"] == 1
    assert report["examples"][0]["was"] == "allow"


async def test_a_replay_with_no_traffic_says_so_rather_than_reporting_safe(
        session, tenant):
    """"Nothing would change" and "nothing happened" are different sentences,
    and only one of them is evidence."""
    from case_service.hxarbiter.nlp import whatif

    report = await whatif.replay(
        session,
        rules=[_rule("r", "deny", {"attr": "context.provider_class",
                                   "op": "eq", "value": "external"})],
        namespace="egress", tenant_id=tenant.id, catalog=VELARIS_CATALOG)
    assert report["replayed"] is False
    assert report["reason"] == "no_traffic"


async def test_a_replay_cannot_be_fooled_by_a_spoofed_structural_attribute(
        session, tenant):
    """`_attrs_from` mirrors `RequestResolver`, structural fields winning, so a
    replay cannot reach a conclusion the live path would not have."""
    from case_service.hxarbiter.nlp import whatif

    row = ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", action="ai.x",
        subject_kind="user", subject_id="real",
        subject={"kind": "user", "id": "real", "attrs": {"kind": "service",
                                                         "id": "fake"}},
        resource={}, context={}, effect="allow", mode="shadow", enforced=False,
        default_applied=True, matched=[], obligations=[],
        evaluated_at=datetime.now(timezone.utc))
    attrs = whatif._attrs_from(row)
    assert attrs["subject.kind"] == "user"
    assert attrs["subject.id"] == "real"


# ══ observability ═══════════════════════════════════════════════════════════


async def test_a_decision_emits_metrics(session, tenant):
    """§13 sets latency targets and nothing emitted a metric, so an operator
    could not alert on the three things that actually go wrong: a namespace
    suddenly denying, PIPs timing out, or the log not being sealed."""
    from velaris_shared.observability import metrics as m

    before = m.registry.get_sample_value(
        "helix_arbiter_decisions_total",
        {"tenant_id": "default", "namespace": "ai", "effect": "deny",
         "enforced": "false"}) or 0.0

    await pep.check(session, tenant_id="default", action="ai.external_call",
                    subject_id="metrics-probe",
                    context={"provider_class": "external"}, legacy_allowed=True)

    after = m.registry.get_sample_value(
        "helix_arbiter_decisions_total",
        {"tenant_id": "default", "namespace": "ai", "effect": "deny",
         "enforced": "false"}) or 0.0
    assert after == before + 1


async def test_a_disagreement_is_counted_by_DIRECTION(session, tenant):
    """One "agreement %" hides which failure you have. `engine_weaker` is a
    hole and must be zero before a promotion; `engine_stricter` is an outage if
    promoted. They are separate series."""
    from velaris_shared.observability import metrics as m

    before = m.registry.get_sample_value(
        "helix_arbiter_legacy_disagreements_total",
        {"namespace": "ai", "direction": "engine_stricter"}) or 0.0

    # `ai` defaults to deny and no policy matches, so the engine denies where
    # the legacy control allowed.
    await pep.check(session, tenant_id="default", action="ai.external_call",
                    subject_id="direction-probe", legacy_allowed=True)

    after = m.registry.get_sample_value(
        "helix_arbiter_legacy_disagreements_total",
        {"namespace": "ai", "direction": "engine_stricter"}) or 0.0
    assert after == before + 1


async def test_metrics_are_not_sampled_with_the_log(session, tenant):
    """Sampling thins the LOG because it is storage. A metric that inherited it
    would under-report the deny rate it exists to alert on."""
    ns = await session.get(ArbiterNamespaceModel, "case")
    ns.sample_allow_rate = 0        # keep no allow rows at all
    ns.sample_default_rate = 0
    await session.commit()
    pdp.invalidate_cache()

    from velaris_shared.observability import metrics as m
    label = {"tenant_id": "default", "namespace": "case", "effect": "allow",
             "enforced": "false"}
    before = m.registry.get_sample_value(
        "helix_arbiter_decisions_total", label) or 0.0

    await pep.check(session, tenant_id="default",
                    action="case.stage.transition", subject_id="sampled",
                    legacy_allowed=True)
    await session.commit()

    after = m.registry.get_sample_value(
        "helix_arbiter_decisions_total", label) or 0.0
    assert after == before + 1, "the metric was sampled away with the log row"
    assert (await session.execute(
        select(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.subject_id == "sampled")
    )).scalars().first() is None, "the log row should have been sampled away"


# ══ authorization before rate limiting ══════════════════════════════════════


async def test_authorization_is_resolved_before_the_rate_limiter():
    """FastAPI resolves dependencies in declaration order.

    A limiter declared before the guard runs first, so a caller holding none of
    these actions got a **429 rather than a 403** — which leaks that the route
    exists, and lets an unprivileged caller exhaust the bucket for a privileged
    one sharing an egress IP (the limiter keys on the client address, and an
    office or a VPN is one address).

    Checked on the built signatures rather than over HTTP, because the limiter
    disables itself under pytest — so an HTTP test would pass whichever order
    the dependencies were in, which is exactly how this survived since ARB-A.
    """
    import inspect
    from fastapi.routing import APIRoute
    from case_service.main import app

    offenders = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "/arbiter" not in route.path:
            continue
        params = list(inspect.signature(route.endpoint).parameters)
        if "_rl" not in params:
            continue
        guards = [p for p in params if p == "user"]
        if not guards:
            continue
        if params.index("_rl") < params.index("user"):
            offenders.append(f"{sorted(route.methods)[0]} {route.path}")
    assert not offenders, (
        f"these routes rate-limit before they authorize: {offenders}")


# ══ retention must not destroy the tamper-evidence ══════════════════════════


async def _sealed_batch(session, tenant, *, n=3):
    """n decisions that are ALREADY old, then sealed. Order matters twice.

    * `evaluated_at` is inside `compute_batch_hash`, so backdating a row after
      sealing breaks the seal by itself — the first version of this helper did
      exactly that and reported a broken chain before any purge ran.
    * `seal_once` resumes from the chain TIP and walks forward in 60-second
      windows, so a row dated behind the tip can never be sealed at all.

    25 hours: old enough to be past a 1-day horizon, and reachable in two
    passes of the 1440-batch-per-run ceiling.
    """
    from datetime import timedelta as _td

    old = datetime.now(timezone.utc) - _td(hours=25)
    rows = []
    for i in range(n):
        row = ArbiterDecisionModel(
            id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai",
            action="ai.external_call", subject_kind="service",
            subject_id=f"tomb-{uuid.uuid4().hex[:8]}", subject={}, resource={},
            context={}, effect="deny", mode="shadow", enforced=False,
            default_applied=True, matched=[], obligations=[],
            evaluated_at=old + _td(seconds=i))
        session.add(row); rows.append(row)
    await session.commit()
    # Expire everything before sealing. `compute_batch_hash` covers
    # `evaluated_at`, and a session that still holds the AWARE datetime this
    # helper assigned hashes it as "…+00:00" while a later fresh read on a
    # naive-datetime backend hashes it as "…" — so the seal and the verify
    # disagree for a reason that has nothing to do with tampering.
    #
    # Real code never hits this: `seal_once` and `verify_chain` both read the
    # rows back from the database, so both see the driver's representation.
    # Recorded here because a test that constructs rows in-session does not.
    session.expire_all()
    for _ in range(3):
        if not await chain.seal_once(session):
            break
        await session.commit()
    await session.commit()
    for row in rows:
        await session.refresh(row)
    return rows


async def test_purging_sealed_decisions_does_not_break_the_chain(
        session, tenant):
    """The defect migration 180 shipped, found only by running a purge.

    Deleting the rows a sealed batch covers made `verify_chain` fail forever —
    *"batch 1 sealed 6 decisions and now holds 0"* — so switching retention on
    destroyed the tamper-evidence the product is sold on. 180's reasoning was
    half right: it refused to delete UNSEALED rows because that looks like
    tampering, and missed that the SEALED ones are the ones whose deletion
    actually breaks the seal.
    """
    rows = await _sealed_batch(session, tenant)
    assert all(r.chain_sequence is not None for r in rows), "setup: not sealed"
    ok, _ = await chain.verify_chain(session)
    assert ok, "setup: the chain did not verify before the purge"

    ns = await session.get(ArbiterNamespaceModel, "ai")
    ns.decision_retention_days = 1
    await session.commit()

    purged = await chain.purge_expired_decisions(session)
    await session.commit()
    assert purged.get("ai") == len(rows)

    ok, note = await chain.verify_chain(session)
    assert ok, f"a retention purge broke the chain: {note}"
    assert note and "pruned by retention" in note, (
        "a pruned window must be REPORTED, not silently accepted — "
        '"it verifies" and "it verifies, and these windows were emptied" are '
        "different answers")


async def test_a_pruned_batch_is_tombstoned_with_a_count_and_a_time(
        session, tenant):
    from velaris_shared.db.models import ArbiterDecisionChainModel

    rows = await _sealed_batch(session, tenant, n=4)
    ns = await session.get(ArbiterNamespaceModel, "ai")
    ns.decision_retention_days = 1
    await session.commit()
    await chain.purge_expired_decisions(session)
    await session.commit()

    batch = (await session.execute(
        select(ArbiterDecisionChainModel)
        .where(ArbiterDecisionChainModel.sequence == rows[0].chain_sequence)
    )).scalars().first()
    assert batch.pruned_at is not None
    assert batch.pruned_count == 4
    # The hash is KEPT. It still proves what the batch was, and it is what the
    # next batch links to — re-sealing over the new contents would let anyone
    # erase evidence and leave a chain saying everything is fine.
    assert batch.content_hash


async def test_an_unpruned_batch_that_lost_rows_is_still_tampering(
        session, tenant):
    """The tombstone must not become a blanket excuse. A batch that lost rows
    WITHOUT being pruned is exactly what the chain exists to catch."""
    from sqlalchemy import delete as _delete

    rows = await _sealed_batch(session, tenant, n=3)
    await session.execute(
        _delete(ArbiterDecisionModel).where(ArbiterDecisionModel.id == rows[0].id))
    await session.commit()

    ok, problem = await chain.verify_chain(session)
    assert ok is False
    assert "now holds" in problem


async def test_retention_still_refuses_to_touch_an_unsealed_row(
        session, tenant):
    """Unchanged from 180, and still right: a row the chain has not covered
    has no tombstone to carry, so deleting it would leave a gap that reads as
    tampering."""
    from datetime import timedelta as _td

    session.add(ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", action="ai.x",
        subject_kind="service", subject_id="unsealed", subject={}, resource={},
        context={}, effect="deny", mode="shadow", enforced=False,
        default_applied=True, matched=[], obligations=[],
        chain_sequence=None,
        evaluated_at=datetime.now(timezone.utc) - _td(days=200)))
    ns = await session.get(ArbiterNamespaceModel, "ai")
    ns.decision_retention_days = 1
    await session.commit()

    assert await chain.purge_expired_decisions(session) == {}


# ══ the soak must not cost a query per decision ═════════════════════════════


async def test_the_soak_lookup_is_cached_not_run_per_decision(session, tenant):
    """It ran an uncached SELECT with a join on EVERY recorded decision — one
    database round trip added to the hot path to observe a rule nobody is
    enforcing.

    Invisible in the engine's own numbers, too: `latency_us` is stamped by
    `_finish()` before the soak runs, so the decision row reported a time that
    excluded the cost this added. The load test could not see it either — it
    measures the pure `Evaluator`, and this lives in `pdp.decide` around it.
    """
    calls = {"n": 0}
    real = store.load_soaking_policies

    async def counting(*a, **kw):
        calls["n"] += 1
        return await real(*a, **kw)

    store.load_soaking_policies = counting
    try:
        pdp.invalidate_cache()
        for i in range(5):
            await pep.check(
                session, tenant_id="default", action="ai.external_call",
                subject_id=f"cache-{i}", context={"provider_class": "external"},
                legacy_allowed=True)
        await session.commit()
    finally:
        store.load_soaking_policies = real

    assert calls["n"] == 1, (
        f"the soak set was loaded {calls['n']} times for 5 decisions; it must "
        f"be cached like the active set")


async def test_the_soak_cache_is_keyed_on_the_hour_not_only_the_generation(
        session, tenant):
    """The soak window is defined by TIME, not by rows.

    A policy leaves it 14 days after retirement without anything being
    written, so a purely generation-keyed cache would go on observing it
    forever — the retirement soak would never end.
    """
    import time as _time

    pdp.invalidate_cache()
    await pdp._cache.soaking(
        session, tenant_id=tenant.id, namespace="ai", catalog=VELARIS_CATALOG)
    keys = list(pdp._cache._soaking)
    assert keys, "nothing was cached"
    assert keys[0][2] == int(_time.time() // 3600), (
        "the cache key must carry the hour bucket")


# ══ the soak window, at its edges and across namespaces ═════════════════════


async def _retired_days_ago(session, tenant, namespace, days, rule_id="r"):
    p = await _activate(
        session, tenant, f"soak-{namespace}-{uuid.uuid4().hex[:6]}",
        [_rule(rule_id, "deny", {"attr": "subject.kind", "op": "is_present"})],
        namespace=namespace)
    await store.retire_policy(session, policy_id=p.id)
    row = await session.get(ArbiterPolicyModel, p.id)
    row.retired_at = datetime.now(timezone.utc) - timedelta(days=days)
    await session.commit()
    pdp.invalidate_cache()
    return p


async def _soak_events(session):
    return (await session.execute(
        select(SecurityEventModel)
        .where(SecurityEventModel.event_type == "arbiter_retirement_soak")
    )).scalars().all()


@pytest.mark.parametrize("namespace,action", [
    ("ai", "ai.external_call"),
    ("egress", "egress.http"),
    ("export", "export.case_data"),
    ("vault", "vault.dek.shred"),
    ("case", "case.stage.transition"),
    ("auth", "auth.session.elevate"),
])
async def test_the_soak_is_observed_in_every_namespace(
        session, tenant, namespace, action):
    """Proven per namespace, not just `ai`.

    Each carries its own default effect, and the soak evaluates the retired
    rules in their own pass with `namespace_default=ALLOW` so that "nothing
    matched" is never recorded as a hit. A namespace whose real default is
    ALLOW is where that could go wrong unnoticed.
    """
    await _retired_days_ago(session, tenant, namespace, days=1)
    await pep.check(session, tenant_id="default", action=action,
                    subject_id="u", subject_kind=pep.SubjectKind.USER,
                    legacy_allowed=True)
    await session.commit()

    events = await _soak_events(session)
    assert events, f"{namespace}: the retired rule's soak was not observed"
    assert events[0].details["namespace"] == namespace
    assert events[0].details["would_have"] == "deny"


async def test_the_soak_window_boundary_is_exact(session, tenant):
    """Just inside the window is observed; just outside is not.

    Waiting fourteen real days proves nothing this does not — the boundary is
    the property, and it moves with the clock rather than with any write, which
    is why the cache is keyed on the hour.
    """
    inside = store.RETIREMENT_SOAK_DAYS - 1
    outside = store.RETIREMENT_SOAK_DAYS + 1

    await _retired_days_ago(session, tenant, "ai", days=inside)
    await pep.check(session, tenant_id="default", action="ai.external_call",
                    subject_id="u", subject_kind=pep.SubjectKind.USER,
                    legacy_allowed=True)
    await session.commit()
    assert await _soak_events(session), (
        f"a policy retired {inside} days ago is inside the "
        f"{store.RETIREMENT_SOAK_DAYS}-day window and must be observed")

    # Move it past the edge; the same request must now record nothing new.
    for row in (await session.execute(select(ArbiterPolicyModel))).scalars():
        if row.retired_at is not None:
            row.retired_at = datetime.now(timezone.utc) - timedelta(days=outside)
    await session.commit()
    pdp.invalidate_cache()

    before = len(await _soak_events(session))
    await pep.check(session, tenant_id="default", action="ai.external_call",
                    subject_id="u2", subject_kind=pep.SubjectKind.USER,
                    legacy_allowed=True)
    await session.commit()
    assert len(await _soak_events(session)) == before, (
        f"a policy retired {outside} days ago is past the window and must no "
        f"longer be observed — otherwise the soak never ends")
