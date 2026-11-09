"""F-07 (first slice) — the sovereign model marketplace.

The tests that matter are the ones where the exchange refuses, because each one
is a way a shared model could quietly become something other than weights:

* the FORMAT is the boundary — a payload with anywhere to hide a blob is
  refused before anything reads it;
* a file edited after signing does not verify, and the hash is checked before
  the payload is ever interpreted;
* a model trained on too few cases is **not publishable**, because its
  published means are the cases themselves;
* a feature with no spread republishes one value the whole cohort shared;
* an Article 9 term in a signal pack's own rule text blocks the publish;
* a stranger's model is scored on THIS tenant's data before it may be
  installed, and "we have nothing to test it on" is not a failure;
* nothing imported ever runs — a risk model lands pending, a pack lands
  disabled;
* uninstall-with-delete destroys the weights and keeps the record.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import copy
import json
import uuid

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseRiskModelModel,
    CaseRiskSignalPackModel,
    CaseTypeModel,
    ModelPackageInstallModel,
    ModelPackageModel,
    TenantModel,
)

from commerce_service.modelmarket import canary, evaluate as ev, service
from velaris_shared.modelmarket import packaging
from case_service.sentinel.model import MODEL_FEATURES

pytestmark = pytest.mark.asyncio

PREFIX = "/api/v1/model-market"


def good_payload(**over) -> dict:
    payload = {
        "features": list(MODEL_FEATURES),
        "coefficients": {f: 0.4 + i * 0.1 for i, f in enumerate(MODEL_FEATURES)},
        "intercept": -0.3,
        "feature_means": {f: 100.0 + i for i, f in enumerate(MODEL_FEATURES)},
        "feature_stds": {f: 20.0 + i for i, f in enumerate(MODEL_FEATURES)},
        "threshold": 0.5,
    }
    payload.update(over)
    return payload


def good_pack(**over) -> dict:
    pack = {
        "id": "acme/fraud-signals",
        "name": "Fraud signals",
        "version": "1.0.0",
        "rules": [{"key": "big_amount", "severity": "high", "title": "Large amount",
                   "message": "The claim is unusually large.",
                   "when": "amount > 10000"}],
        "taxonomy": [],
    }
    pack.update(over)
    return pack


@pytest_asyncio.fixture
async def tenant(session):
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    _identity.clear_cache()


# ── the format is the boundary ───────────────────────────────────────────────


def test_extra_field_is_refused():
    """An extra key is exactly how a smuggled field arrives."""
    with pytest.raises(packaging.PackageError, match="forbids"):
        packaging.scan_risk_model(good_payload(notes="anything at all"))


def test_no_string_field_exists_to_hide_a_blob_in():
    """The closed feature vocabulary is what makes this structural.

    There is no key in the payload a base64 pickle could occupy, so the refusal
    does not depend on recognising the blob.
    """
    blob = base64.b64encode(b"\x80\x04\x95pickle").decode()
    for attempt in (
        good_payload(intercept=blob),
        good_payload(threshold=blob),
        good_payload(coefficients={**good_payload()["coefficients"], "amount": blob}),
        good_payload(features=list(MODEL_FEATURES) + [blob]),
    ):
        with pytest.raises(packaging.PackageError):
            packaging.scan_risk_model(attempt)


def test_a_different_feature_set_is_refused():
    with pytest.raises(packaging.PackageError, match="feature set"):
        packaging.scan_risk_model(good_payload(
            features=["amount", "something_else", "phone_velocity",
                      "email_velocity", "address_velocity"]))


def test_zero_standard_deviation_is_refused():
    """score_case divides by this; a zero makes every case score inf or NaN."""
    stds = {f: 20.0 for f in MODEL_FEATURES}
    stds["amount"] = 0.0
    with pytest.raises(packaging.PackageError, match="divide"):
        packaging.scan_risk_model(good_payload(feature_stds=stds))


def test_non_finite_and_out_of_range_are_refused():
    for bad in (float("nan"), float("inf"), 1e9):
        with pytest.raises(packaging.PackageError):
            packaging.scan_risk_model(good_payload(intercept=bad))


def test_threshold_must_be_a_probability():
    with pytest.raises(packaging.PackageError, match="probability"):
        packaging.scan_risk_model(good_payload(threshold=1.4))


def test_a_pack_with_an_unsafe_expression_is_refused():
    """The boundary is HxSentinel's own, not a second weaker opinion."""
    with pytest.raises(packaging.PackageError):
        packaging.scan_signal_pack(good_pack(rules=[
            {"key": "evil", "severity": "high", "title": "t", "message": "m",
             "when": "__import__('os').system('id')"}]))


# ── the file ─────────────────────────────────────────────────────────────────


def test_round_trip_unsigned():
    doc = packaging.build_file(kind="risk_model", package_id="acme/triage",
                               version=1, manifest={"name": "t"},
                               payload=good_payload(), dek=None)
    read = packaging.read_file(json.dumps(doc))
    assert read["content_sha256"] == doc["content_sha256"]
    assert read["signed"] is False
    assert read["signature_reason"] == "unsigned"


def test_a_file_edited_after_signing_is_refused():
    doc = packaging.build_file(kind="risk_model", package_id="acme/triage",
                               version=1, manifest={"name": "t"},
                               payload=good_payload(), dek=b"k" * 32)
    tampered = copy.deepcopy(doc)
    tampered["payload"]["intercept"] = 9.0
    with pytest.raises(packaging.PackageError, match="altered"):
        packaging.read_file(json.dumps(tampered))


def test_signature_round_trips_and_names_its_key():
    doc = packaging.build_file(kind="risk_model", package_id="acme/triage",
                               version=1, manifest={"name": "t"},
                               payload=good_payload(), dek=b"k" * 32)
    read = packaging.read_file(json.dumps(doc))
    assert read["signed"] is True
    assert read["signing_key_id"] == doc["signature"]["key_id"]


def test_a_lied_about_key_id_does_not_verify():
    """An operator pinning `key_id` must not be pinning a lie."""
    doc = packaging.build_file(kind="risk_model", package_id="acme/triage",
                               version=1, manifest={"name": "t"},
                               payload=good_payload(), dek=b"k" * 32)
    doc["signature"]["key_id"] = "0" * 32
    read = packaging.read_file(json.dumps(doc))
    assert read["signed"] is False
    assert "key_id" in read["signature_reason"]


def test_an_unknown_format_is_refused_rather_than_parsed_hopefully():
    doc = packaging.build_file(kind="risk_model", package_id="acme/triage",
                               version=1, manifest={}, payload=good_payload(),
                               dek=None)
    doc["format"] = "velaris.model/99"
    with pytest.raises(packaging.PackageError, match="format"):
        packaging.read_file(json.dumps(doc))


# ── the canary ───────────────────────────────────────────────────────────────


def test_a_small_cohort_blocks_publication():
    checks = canary.cohort_checks(trained_on=6, metrics={"positives": 3, "negatives": 3})
    assert [c["ok"] for c in checks] == [False, False]


def test_a_feature_with_no_spread_is_one_value_the_cohort_shared():
    payload = good_payload(feature_means={f: 4300.0 for f in MODEL_FEATURES},
                           feature_stds={f: 0.5 for f in MODEL_FEATURES})
    checks = canary.aggregate_disclosure(packaging.scan_risk_model(payload))
    assert all(c["ok"] is False for c in checks)


def test_a_zero_centre_is_not_treated_as_disclosure():
    """The velocity features legitimately sit at zero on a quiet tenant."""
    payload = good_payload(feature_means={f: 0.0 for f in MODEL_FEATURES},
                           feature_stds={f: 1.0 for f in MODEL_FEATURES})
    checks = canary.aggregate_disclosure(packaging.scan_risk_model(payload))
    assert all(c["ok"] for c in checks)


async def test_article_9_text_in_a_pack_blocks_it(session, tenant):
    """Free text is where a special category actually reaches a shared artefact."""
    pack = good_pack(rules=[{
        "key": "health_flag", "severity": "high",
        "title": "Diagnosis recorded",
        "message": "The claimant's medical condition raises the risk.",
        "when": "amount > 1"}])
    is_special, check, _ = await canary.special_category(
        session, tenant_token="default", kind=packaging.KIND_SIGNAL_PACK,
        payload=pack)
    assert is_special is True and check["ok"] is False


async def test_a_risk_model_cannot_be_special_category_by_construction(session, tenant):
    is_special, check, classes = await canary.special_category(
        session, tenant_token="default", kind=packaging.KIND_RISK_MODEL,
        payload=good_payload())
    assert is_special is False
    assert "Article 9" in check["detail"]
    assert set(classes) == {"financial", "phone", "email", "address"}


# ── the eval gate ────────────────────────────────────────────────────────────


def _synthetic(n_pos: int, n_neg: int, *, separable: bool):
    rng = np.random.default_rng(7)
    pos = rng.normal(160, 20, size=(n_pos, len(MODEL_FEATURES)))
    neg = rng.normal(60 if separable else 160, 20, size=(n_neg, len(MODEL_FEATURES)))
    X = np.vstack([pos, neg])
    y = np.concatenate([np.ones(n_pos), np.zeros(n_neg)])
    return X, y


async def test_no_labelled_history_is_not_a_failure(session, tenant, monkeypatch):
    async def _none(*_a, **_k):
        return np.zeros((0, len(MODEL_FEATURES))), np.zeros(0)

    monkeypatch.setattr("case_service.sentinel.model._labelled_features", _none)
    result = await ev.evaluate(session, tenant_token="default", tenant_uuid=tenant.id,
                               kind="risk_model",
                               payload=good_payload(), declared={"holdout_auc": 0.9})
    assert result["verdict"] == ev.VERDICT_INSUFFICIENT
    assert "Nothing is wrong with the package" in result["reason"]


async def test_a_model_that_does_not_transfer_is_refused(session, tenant, monkeypatch):
    async def _noise(*_a, **_k):
        return _synthetic(30, 30, separable=False)

    monkeypatch.setattr("case_service.sentinel.model._labelled_features", _noise)
    result = await ev.evaluate(session, tenant_token="default", tenant_uuid=tenant.id,
                               kind="risk_model",
                               payload=good_payload(), declared={"holdout_auc": 0.95})
    assert result["verdict"] == ev.VERDICT_FAIL


async def test_a_claim_that_reproduces_passes(session, tenant, monkeypatch):
    async def _clean(*_a, **_k):
        return _synthetic(30, 30, separable=True)

    monkeypatch.setattr("case_service.sentinel.model._labelled_features", _clean)
    result = await ev.evaluate(session, tenant_token="default", tenant_uuid=tenant.id,
                               kind="risk_model",
                               payload=good_payload(), declared={"holdout_auc": 0.95})
    assert result["verdict"] == ev.VERDICT_PASS
    assert result["measured"]["holdout_auc"] >= ev.MIN_USEFUL_AUC


async def test_an_inflated_claim_is_refused_even_when_the_model_works(
        session, tenant, monkeypatch):
    """Working is not the same as the card being right."""
    async def _mid(*_a, **_k):
        rng = np.random.default_rng(3)
        pos = rng.normal(120, 40, size=(40, len(MODEL_FEATURES)))
        neg = rng.normal(90, 40, size=(40, len(MODEL_FEATURES)))
        return np.vstack([pos, neg]), np.concatenate([np.ones(40), np.zeros(40)])

    monkeypatch.setattr("case_service.sentinel.model._labelled_features", _mid)
    result = await ev.evaluate(session, tenant_token="default", tenant_uuid=tenant.id,
                               kind="risk_model",
                               payload=good_payload(), declared={"holdout_auc": 0.999},
                               tolerance=0.02)
    assert result["verdict"] == ev.VERDICT_FAIL
    assert "does not reproduce" in result["reason"]


# ── publishing ───────────────────────────────────────────────────────────────


async def test_publishing_needs_an_explicit_acknowledgement(session, tenant):
    with pytest.raises(service.ModelMarketError, match="acknowledge"):
        await service.publish(session, tenant_token="default", kind="risk_model",
                              source="1", package_id="acme/triage", actor="u",
                              acknowledged=False)


async def test_a_blocked_publish_leaves_a_record(session, tenant, monkeypatch):
    """The refusal is the record. Raising would unwind the row that holds it."""
    session.add(CaseRiskModelModel(
        id=uuid.uuid4(), tenant_id="default", version=1,
        coefficients=good_payload()["coefficients"], intercept=-0.3,
        feature_means={f: 4300.0 for f in MODEL_FEATURES},
        feature_stds={f: 0.5 for f in MODEL_FEATURES},
        threshold=0.5, trained_on=4,
        metrics={"holdout_auc": 0.8, "positives": 2, "negatives": 2},
        status="active"))
    await session.commit()

    row = await service.publish(
        session, tenant_token="default", kind="risk_model", source="1",
        package_id="acme/triage", actor="u", acknowledged=True)
    await session.commit()

    assert row.status == "quarantined"
    assert "cohort" in (row.status_reason or "") or "spread" in (row.status_reason or "")
    kept = (await session.execute(
        select(ModelPackageModel).where(ModelPackageModel.id == row.id))).scalars().first()
    assert kept is not None and kept.canary_verdict == "blocked"


async def _publishable_model(session, monkeypatch):
    session.add(CaseRiskModelModel(
        id=uuid.uuid4(), tenant_id="default", version=1,
        coefficients=good_payload()["coefficients"], intercept=-0.3,
        feature_means=good_payload()["feature_means"],
        feature_stds=good_payload()["feature_stds"],
        threshold=0.5, trained_on=60,
        metrics={"holdout_auc": 0.82, "positives": 25, "negatives": 35},
        status="active"))
    await session.commit()

    async def _clean(*_a, **_k):
        return _synthetic(30, 30, separable=True)

    monkeypatch.setattr("case_service.sentinel.model._labelled_features", _clean)


async def test_the_card_says_what_leaves_with_the_file(session, tenant, monkeypatch):
    """A card that lists metrics and stays quiet about the aggregates reads as
    more private than the artefact is."""
    await _publishable_model(session, monkeypatch)
    prepared = await service.prepare(
        session, tenant_token="default", kind="risk_model", source="1")
    contains = " ".join(prepared["manifest"]["disclosure"]["contains"])
    assert "MEAN" in contains and "STANDARD DEVIATION" in contains
    assert prepared["manifest"]["disclosure"]["cohort_size"] == 60


# ── the round trip ───────────────────────────────────────────────────────────


async def test_a_published_row_is_signed_before_anybody_downloads_it(
        session, tenant, monkeypatch):
    """A row that says "unsigned" until somebody happens to export it tells
    every listing something untrue about itself in the meantime."""
    await _publishable_model(session, monkeypatch)
    row = await service.publish(
        session, tenant_token="default", kind="risk_model", source="1",
        package_id="acme/triage", actor="publisher", acknowledged=True)
    assert row.status == "published"
    assert row.signing_key_id is not None


async def test_import_install_and_shred(session, tenant, monkeypatch):
    await _publishable_model(session, monkeypatch)
    published = await service.publish(
        session, tenant_token="default", kind="risk_model", source="1",
        package_id="acme/triage", actor="publisher", acknowledged=True)
    assert published.status == "published"
    doc = await service.export_file(session, published)
    await session.commit()

    imported = await service.import_file(
        session, tenant_token="default", raw=json.dumps(doc), actor="importer")
    await session.commit()
    assert imported.direction == "imported"
    # The publisher's canary ran on the publisher's data; it is not evidence here.
    assert imported.canary_verdict == "not_run"

    # Re-importing the same bytes is the same package, not a second opinion.
    again = await service.import_file(
        session, tenant_token="default", raw=json.dumps(doc), actor="importer")
    assert again.id == imported.id

    with pytest.raises(service.ModelMarketError, match="not been evaluated"):
        await service.install(session, tenant_token="default", package=imported,
                              actor="importer")

    ev_row = await service.run_eval(session, tenant_token="default",
                                    package=imported, actor="importer")
    assert ev_row.verdict == ev.VERDICT_PASS
    install = await service.install(session, tenant_token="default",
                                    package=imported, actor="importer")
    await session.commit()

    assert install.status == "staged"
    assert install.model_origin == "marketplace"

    kind, _, ref = install.target_ref.partition(":")
    landed = await session.get(CaseRiskModelModel, uuid.UUID(ref))
    # Nothing imported runs until a person switches it on.
    assert landed.status == "pending"
    # The importer's own measurement is what their screens show.
    assert landed.metrics["origin"] == "marketplace"
    assert landed.metrics["holdout_auc"] == ev_row.measured["holdout_auc"]
    assert landed.metrics["declared"]["holdout_auc"] == 0.82
    # The publisher's cohort is not printed as this tenant's.
    assert landed.trained_on == 0
    assert landed.metrics["publisher_trained_on"] == 60

    await service.uninstall(session, tenant_token="default", install_row=install,
                            shred=True, actor="importer")
    await session.commit()

    pkg = await session.get(ModelPackageModel, imported.id)
    assert pkg.payload == {"shredded": True}
    # The record survives the weights.
    kept = await session.get(ModelPackageInstallModel, install.id)
    assert kept.status == "shredded" and kept.shredded_at is not None
    assert (await session.get(CaseRiskModelModel, uuid.UUID(ref))).status == "archived"


async def _some_cases(session, tenant, n=25):
    """Enough cases for a pack dry-run to be a measurement rather than a skip."""
    ct = CaseTypeModel(id=uuid.uuid4(), name="Claims", version="1.0.0",
                       lifecycle_process_id="p", definition_json={"stages": []},
                       tenant_id=str(tenant.id))
    session.add(ct)
    await session.flush()
    for i in range(n):
        session.add(CaseInstanceModel(
            id=uuid.uuid4(), case_type_id=ct.id, case_type_version="1.0.0",
            status="new", priority="medium", tenant_id=tenant.id,
            data={"amount": 1000 * (i + 1)}))
    await session.commit()


async def test_a_pack_is_not_gated_in_on_a_sample_of_one(session, tenant):
    """Found live. Three rules that parse over ONE case proves the expressions
    compile and nothing whatever about how they behave here."""
    await _some_cases(session, tenant, n=1)
    result = await ev.evaluate(session, tenant_token="default", tenant_uuid=tenant.id,
                               kind="signal_pack", payload=good_pack(), declared=None)
    assert result["verdict"] == ev.VERDICT_INSUFFICIENT
    assert "Nothing is wrong with the package" in result["reason"]


async def test_an_imported_signal_pack_lands_disabled(session, tenant, monkeypatch):
    """`install_pack` enables what it installs — right for a catalogue choice,
    wrong for a stranger's rules arriving in a file."""
    await _some_cases(session, tenant)
    session.add(CaseRiskSignalPackModel(
        id=uuid.uuid4(), tenant_id="default", pack_id="acme/fraud-signals",
        name="Fraud signals", version="1.0.0", manifest=good_pack(),
        enabled=True, installed_by="u"))
    await session.commit()

    published = await service.publish(
        session, tenant_token="default", kind="signal_pack",
        source="acme/fraud-signals", package_id="acme/fraud-signals",
        actor="publisher", acknowledged=True)
    assert published.status == "published"
    doc = await service.export_file(session, published)
    await session.commit()

    imported = await service.import_file(
        session, tenant_token="default", raw=json.dumps(doc), actor="importer")
    ev_row = await service.run_eval(session, tenant_token="default",
                                    package=imported, actor="importer")
    assert ev_row.verdict == ev.VERDICT_PASS, ev_row.reason
    install = await service.install(session, tenant_token="default",
                                    package=imported, actor="importer")
    await session.commit()
    _, _, ref = install.target_ref.partition(":")
    assert (await session.get(CaseRiskSignalPackModel, uuid.UUID(ref))).enabled is False


# ── the API ──────────────────────────────────────────────────────────────────


async def test_routes_are_gated(anon_client):
    for path in ("/sources", "/packages"):
        resp = await anon_client.get(PREFIX + path)
        assert resp.status_code in (401, 403), f"GET {path} -> {resp.status_code}"
    for path in ("/prepare", "/publish", "/import"):
        resp = await anon_client.post(PREFIX + path, json={})
        assert resp.status_code in (401, 403), f"POST {path} -> {resp.status_code}"


async def test_sources_lists_what_could_be_shared(client, session, tenant, monkeypatch):
    await _publishable_model(session, monkeypatch)
    resp = await client.get(PREFIX + "/sources")
    assert resp.status_code == 200, resp.text
    assert resp.json()["risk_models"][0]["source"] == "1"


async def test_import_rejects_a_malformed_file(client, tenant):
    resp = await client.post(PREFIX + "/import", json={"package": "{not json"})
    assert resp.status_code == 400
    assert "VEL-MODELMARKET-IMPORT-400" in resp.text
