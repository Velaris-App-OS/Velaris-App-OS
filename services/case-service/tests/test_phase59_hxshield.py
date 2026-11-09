"""Tests for P59 HxShield — fraud & abuse detection."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.conftest import client  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_create_rule(client: AsyncClient):
    resp = await client.post("/api/v1/shield/rules", json={
        "name": "DoS submission guard",
        "pattern_type": "dos_submission",
        "threshold": 5,
        "window_seconds": 60,
        "action": "block",
        "severity": "high",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["pattern_type"] == "dos_submission"
    assert data["action"] == "block"
    return data["id"]


@pytest.mark.asyncio
async def test_list_rules(client: AsyncClient):
    await client.post("/api/v1/shield/rules", json={
        "name": "Velocity guard",
        "pattern_type": "velocity_anomaly",
        "threshold": 20,
        "window_seconds": 300,
        "action": "flag",
        "severity": "medium",
    })
    resp = await client.get("/api/v1/shield/rules")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_update_rule(client: AsyncClient):
    cr = await client.post("/api/v1/shield/rules", json={
        "name": "To Update",
        "pattern_type": "replay_attack",
        "threshold": 3,
        "window_seconds": 60,
        "action": "flag",
        "severity": "low",
    })
    rule_id = cr.json()["id"]
    resp = await client.patch(f"/api/v1/shield/rules/{rule_id}", json={"threshold": 10})
    assert resp.status_code == 200
    assert resp.json()["threshold"] == 10


@pytest.mark.asyncio
async def test_delete_rule(client: AsyncClient):
    cr = await client.post("/api/v1/shield/rules", json={
        "name": "To Delete",
        "pattern_type": "duplicate_case_flood",
        "threshold": 5,
        "window_seconds": 60,
        "action": "flag",
        "severity": "low",
    })
    rule_id = cr.json()["id"]
    resp = await client.delete(f"/api/v1/shield/rules/{rule_id}")
    assert resp.status_code == 204
    get_resp = await client.get(f"/api/v1/shield/rules/{rule_id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_score_no_rules(client: AsyncClient):
    resp = await client.post("/api/v1/shield/score", json={
        "event_type": "case_created",
        "actor_id": "user-123",
        "context": {},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["score"] == 0.0
    assert data["action"] == "allow"


@pytest.mark.asyncio
async def test_list_incidents_empty(client: AsyncClient):
    resp = await client.get("/api/v1/shield/incidents")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_events(client: AsyncClient):
    # Scoring always writes a shield event
    await client.post("/api/v1/shield/score", json={
        "event_type": "form_submit",
        "actor_id": "user-abc",
        "context": {"field": "value"},
    })
    resp = await client.get("/api/v1/shield/events")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_stats(client: AsyncClient):
    resp = await client.get("/api/v1/shield/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "open_incidents" in data
    assert "total_incidents" in data
    assert "flagged_events" in data


# ── The wire (roadmap #25) ────────────────────────────────────────────────────
#
# Every test above passes against a detector nothing calls — which is exactly
# how the platform ran for months with eight detection patterns, 3,905 recorded
# security events, and zero incidents. These assert the part that was missing:
# that recording a security event actually *scores* it.

class TestDetectionIsWiredToRealEvents:

    async def test_the_chokepoint_exposes_a_hook(self):
        from velaris_shared.db import security_events as se

        assert hasattr(se, "register_detector")
        assert hasattr(se, "clear_detector")

    async def test_logging_an_event_invokes_the_detector(self, session):
        """The whole defect in one assertion."""
        from velaris_shared.db import security_events as se

        seen = []

        async def _spy(**kw):
            seen.append(kw)

        se.register_detector(_spy)
        try:
            await se.log_security_event(
                session, event_type="auth.login_failed", user_id="u1",
                outcome="failure", ip_address="10.0.0.9",
                details={"tenant_id": "default"},
            )
            await se.drain_detections()
        finally:
            se.clear_detector()

        assert len(seen) == 1, "a recorded security event was never scored"
        assert seen[0]["event_type"] == "auth.login_failed"
        assert seen[0]["actor_id"] == "u1"
        assert seen[0]["details"]["tenant_id"] == "default"

    async def test_a_failing_detector_never_breaks_the_caller(self, session):
        """This runs inside login and authorization denials. A detector that can
        raise turns "we noticed something" into "your request failed"."""
        from velaris_shared.db import security_events as se

        async def _boom(**kw):
            raise RuntimeError("detector exploded")

        se.register_detector(_boom)
        try:
            await se.log_security_event(
                session, event_type="auth.login_failed", user_id="u2")
            await se.drain_detections()
        finally:
            se.clear_detector()
        # No exception is the assertion.

    async def test_the_event_is_still_recorded_when_detection_fails(self, session):
        from sqlalchemy import select

        from velaris_shared.db import security_events as se
        from velaris_shared.db.models import SecurityEventModel

        async def _boom(**kw):
            raise RuntimeError("nope")

        se.register_detector(_boom)
        try:
            await se.log_security_event(
                session, event_type="auth.login_failed", user_id="u3")
            await se.drain_detections()
        finally:
            se.clear_detector()

        rows = (await session.execute(
            select(SecurityEventModel).where(SecurityEventModel.user_id == "u3")
        )).scalars().all()
        assert rows, "detection failure must not cost the audit record"

    async def test_the_caller_does_not_wait_for_detection(self, session):
        """2026-10-19: awaited, the detector's own session made every caller
        hold a connection in an open transaction while waiting for a second one,
        and the audit writes deadlocked the pool under load."""
        import asyncio

        from velaris_shared.db import security_events as se

        release = asyncio.Event()
        ran = []

        async def _slow(**kw):
            await release.wait()
            ran.append(kw["event_type"])

        se.register_detector(_slow)
        try:
            await asyncio.wait_for(se.log_security_event(
                session, event_type="auth.login_failed", user_id="u4"), timeout=2)
            assert ran == []                       # returned before detection ran
            release.set()
            await se.drain_detections()
        finally:
            se.clear_detector()
        assert ran == ["auth.login_failed"]        # and detection still happened

    async def test_detection_is_off_until_switched_on(self, session):
        """Eight rules that have never seen production traffic do not get turned
        on across a fleet by an upgrade."""
        from velaris_shared.config import get_settings

        assert get_settings().hxshield_detect_enabled is False

    async def test_unscored_event_types_are_skipped(self):
        from case_service.hxshield.detector import _is_scored

        assert _is_scored("auth.login_failed")
        assert _is_scored("case.exported")
        assert not _is_scored("platform.heartbeat")

    async def test_the_engine_vocabulary_is_translated(self):
        """The second half of the defect.

        `_score_event` matches exact names — `login`, `export`, `case_created`.
        The platform records `auth.login`, `gdpr.data_exported`, `authz_denied`.
        Wiring the hook without translating would have produced exactly what
        existed before: zero incidents, with more machinery behind them.
        """
        from case_service.hxshield.detector import EVENT_TRANSLATION

        assert EVENT_TRANSLATION["auth.login"] == "login"
        assert EVENT_TRANSLATION["gdpr.data_exported"] == "export"

    async def test_the_detector_passes_the_translated_name(self, session, monkeypatch):
        import case_service.hxshield.detector as det
        from velaris_shared.config import get_settings

        seen = {}

        async def _fake_evaluate(**kw):
            seen.update(kw)
            return {"score": 0.0, "patterns_matched": [], "action": "allow",
                    "incident_id": None, "explanation": None}

        import contextlib

        @contextlib.asynccontextmanager
        async def _same_session():
            yield session

        monkeypatch.setattr(get_settings(), "hxshield_detect_enabled", True,
                            raising=False)
        monkeypatch.setattr("case_service.hxshield.engine.evaluate", _fake_evaluate)
        monkeypatch.setattr("velaris_shared.db.session.get_session_factory",
                            lambda: (lambda: _same_session()), raising=False)

        await det.detect(event_type="auth.login", actor_id="u9",
                         details={"tenant_id": "default"})

        assert seen.get("event_type") == "login", (
            "the engine was handed a name none of its patterns match")
        assert seen.get("actor_id") == "u9"
        assert seen.get("tenant_id") == "default"

    async def test_an_untranslated_event_passes_through_unchanged(self, session, monkeypatch):
        """Dropping it here would be a silent second filter."""
        import contextlib

        import case_service.hxshield.detector as det
        from velaris_shared.config import get_settings

        seen = {}

        async def _fake_evaluate(**kw):
            seen.update(kw)
            return {"score": 0.0, "patterns_matched": [], "action": "allow",
                    "incident_id": None, "explanation": None}

        @contextlib.asynccontextmanager
        async def _same_session():
            yield session

        monkeypatch.setattr(get_settings(), "hxshield_detect_enabled", True,
                            raising=False)
        monkeypatch.setattr("case_service.hxshield.engine.evaluate", _fake_evaluate)
        monkeypatch.setattr("velaris_shared.db.session.get_session_factory",
                            lambda: (lambda: _same_session()), raising=False)

        await det.detect(event_type="authz_denied", actor_id="u10")
        assert seen.get("event_type") == "authz_denied"

    async def test_every_pattern_is_accounted_for(self):
        """Eight patterns, four buckets, no pattern in two of them.

        The buckets exist because the *fixes* differ: write an event, populate a
        context field, or repair a rule. Collapsing them into "not working" is
        how the whole module sat silent for months.
        """
        from case_service.hxshield.detector import (
            PATTERNS_BROKEN_AS_WRITTEN,
            PATTERNS_WITHOUT_A_PRODUCER,
            PATTERNS_WITHOUT_CONTEXT,
        )
        from case_service.hxshield.engine import ALL_PATTERNS

        buckets = [set(PATTERNS_WITHOUT_A_PRODUCER), set(PATTERNS_WITHOUT_CONTEXT),
                   set(PATTERNS_BROKEN_AS_WRITTEN)]
        for i, a in enumerate(buckets):
            assert a <= set(ALL_PATTERNS), "a bucket names a pattern that does not exist"
            for b in buckets[i + 1:]:
                assert not (a & b), f"a pattern is in two buckets: {a & b}"

        live = set(ALL_PATTERNS) - set().union(*buckets)
        assert live == {"duplicate_case_flood", "dos_submission",
                        "off_hours_bulk_access"}, (
            f"the live set changed: {live}. If a producer or context field was "
            "added, shorten the matching bucket; if one was lost, this is the "
            "regression.")

    async def test_the_new_producers_exist(self):
        """The three events added 2026-08-24, pinned at their call sites."""
        import pathlib as _p

        routers = _p.Path(__file__).resolve().parents[1] / "case_service" / "api" / "routers"
        # The portal submission is written by case-service's internal portal API
        # since the portal split (R5e step 8) — portal-service calls it.
        for router, event in (("cases.py", "case_created"),
                              ("../../internal_api/portal.py", "portal_submit"),
                              ("form_submissions.py", "form_submit")):
            src = (routers / router).read_text()
            assert f'event_type="{event}"' in src, (
                f"{router} no longer writes {event}; the patterns matching it "
                "go dead and nothing else will say so.")

    async def test_replay_attack_is_broken_not_merely_unwired(self):
        """Its comparison input lives inside the dict it hashes.

        `form_submit` and `portal_submit` are both written now, so without this
        test the obvious conclusion is that replay_attack went live. It did not:
        `_payload_hash(context)` covers the whole context, so supplying
        `context["recent_hashes"]` changes the hash it is compared against — and
        changes it on every call.
        """
        import inspect

        from case_service.hxshield import engine

        src = inspect.getsource(engine._score_event)
        block = src[src.index("PATTERN_REPLAY_ATTACK"):]
        block = block[:block.index("if hit")]
        assert "_payload_hash(context)" in block
        assert 'context.get("recent_hashes"' in block

        # And the hash really does cover everything handed to it.
        h1 = engine._payload_hash({"a": 1})
        h2 = engine._payload_hash({"a": 1, "recent_hashes": [h1]})
        assert h1 != h2, (
            "if these matched, injecting recent_hashes would be safe and "
            "replay_attack could simply be wired up")

    async def test_account_takeover_cannot_fire_from_a_real_login(self):
        """Pins the subtler dead end, so nobody assumes it is live because the
        event arrives. It needs BOTH flags; the login site sets neither."""
        import inspect

        from case_service.hxshield import engine

        src = inspect.getsource(engine._score_event)
        block = src[src.index("PATTERN_ACCOUNT_TAKEOVER"):]
        block = block[:block.index("elif") if "elif" in block[10:] else len(block)]
        assert 'context.get("new_country"' in block
        assert 'context.get("sensitive_case_access"' in block

    async def test_off_hours_export_is_the_one_that_can_fire(self, session, monkeypatch):
        """And prove it actually does, with the clock inside the window.

        The live box could not show this — it was 19:44 UTC, outside 22:00-06:00
        — so the proof belongs here rather than in a hopeful comment.
        """
        from datetime import datetime, timezone as _tz

        from sqlalchemy import select

        from velaris_shared.db.models import SecurityIncidentModel
        from case_service.hxshield import engine

        cr_rule = await self._make_rule(session)

        class _Clock:
            @staticmethod
            def now(tz=None):
                return datetime(2026, 8, 24, 23, 30, tzinfo=_tz.utc)

        monkeypatch.setattr(engine, "datetime", _Clock)
        engine._EVENT_WINDOWS.clear() if hasattr(engine, "_EVENT_WINDOWS") else None

        for _ in range(3):
            await engine.evaluate(
                event_type="export", actor_id="exfil-actor", tenant_id=None,
                case_type_id=None, context={}, session=session,
            )
        await session.flush()

        rows = (await session.execute(
            select(SecurityIncidentModel).where(
                SecurityIncidentModel.actor_id == "exfil-actor")
        )).scalars().all()
        assert rows, "off_hours_bulk_access did not fire inside its own window"

    async def _make_rule(self, session):
        from velaris_shared.db.models import SecurityRuleModel

        rule = SecurityRuleModel(
            name="off-hours export", pattern_type="off_hours_bulk_access",
            threshold=1, window_seconds=3600, action="alert",
            severity="high", enabled=True,
        )
        session.add(rule)
        await session.flush()
        return rule


class TestProducersDoNotLeakCaseData:
    """A detection signal must not become a second, uncontrolled copy of case
    data.

    `security_events` is deleted by AGE only and `shield_events` is not touched
    by retention at all, so anything a producer copies there outlives an erasure
    request and sits outside HxPrivacy's classification.
    """

    def test_form_submit_records_a_fingerprint_not_the_values(self):
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[1] / "case_service"
               / "api" / "routers" / "form_submissions.py").read_text()
        # Slice to the end of the call, not to the first ")" — that lands
        # inside a getattr and would make this pass on anything.
        block = src[src.index('event_type="form_submit"'):]
        block = block[:block.index("except Exception")]
        assert '"values": body.values' not in block, (
            "submitted form values are being copied into a security event")
        assert "values_sha256" in block

    def test_the_fingerprint_is_stable_and_discriminating(self):
        from case_service.api.routers.form_submissions import _values_fingerprint

        assert _values_fingerprint({"a": 1, "b": 2}) == _values_fingerprint({"b": 2, "a": 1})
        assert _values_fingerprint({"a": 1}) != _values_fingerprint({"a": 2})
        assert "secret" not in _values_fingerprint({"answer": "secret"})

    def test_the_fingerprint_cannot_raise_inside_a_producer(self):
        """A producer that throws on a date would break form submission."""
        from datetime import date
        from decimal import Decimal

        from case_service.api.routers.form_submissions import _values_fingerprint

        assert _values_fingerprint({"d": date(2026, 8, 24), "n": Decimal("1.5")})
        assert _values_fingerprint(None)

    def test_the_other_producers_carry_identifiers_only(self):
        import pathlib

        routers = pathlib.Path(__file__).resolve().parents[1] / "case_service" / "api" / "routers"
        for router, event in (("cases.py", "case_created"),
                              ("../../internal_api/portal.py", "portal_submit")):
            src = (routers / router).read_text()
            block = src[src.index(f'event_type="{event}"'):]
            block = block[:block.index("except Exception")]
            assert "body.data" not in block and "case.data" not in block, (
                f"{router} copies case data into a security event")
