"""HxDR — the standby server's status, alerts and history (R5c step 11 d).

The posture must never call a silent standby healthy: its status file is
refreshed every minute by the standby's own report, so an old file IS the
finding. Not linked is not a finding — a standby is optional.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from case_service.compliance import dr, dr_status
from case_service.compliance.dr_watch import due_notifications

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _codes(res):
    return {f["code"]: f["severity"] for f in res["findings"]}


def _linked(tmp_path, *, age=timedelta(seconds=30), state="streaming", lag=0.01,
            version="3.2.0", standby_version="3.2.0", parts=None, code=None):
    s = tmp_path / ".standby"
    s.mkdir(exist_ok=True)
    (s / "main.json").write_text(json.dumps({"standby": "s2", "state": "linked"}))
    at = (NOW - age).isoformat()
    sync = {"received_at": at, "standby_version": standby_version,
            **{k: {"state": "ok"} for k in ("docs", "recordings", "env", "config", "identity", "vault")},
            "code": code or {"state": "ok"}, **(parts or {})}
    (s / "status.json").write_text(json.dumps({
        "state": state, "checked_at": at, "version": version, "standby_version": standby_version,
        "databases": {"velaris": {"state": state, "replay_lag_seconds": lag}}, "sync": sync}))


def test_no_standby_is_no_finding(tmp_path):
    res = dr_status.standby_status(tmp_path, NOW)
    assert res["linked"] is False and res["findings"] == []


def test_a_healthy_standby_has_no_findings(tmp_path):
    _linked(tmp_path)
    res = dr_status.standby_status(tmp_path, NOW)
    assert res["linked"] and res["findings"] == [] and res["facts"]["standby"] == "s2"


def test_a_silent_standby_is_high_and_nothing_else_is_believed(tmp_path):
    _linked(tmp_path, age=timedelta(minutes=10))
    assert _codes(dr_status.standby_status(tmp_path, NOW)) == {"standby_silent": "high"}


@pytest.mark.parametrize("kw, code", [
    ({"state": "needs_relink"}, "standby_needs_relink"),
    ({"state": "stalled"}, "standby_not_receiving"),
    ({"state": "disconnected"}, "standby_not_receiving"),
    ({"lag": 75.0}, "standby_lagging"),
    ({"parts": {"docs": {"state": "deletes_held", "note": "EMPTY"}}}, "standby_docs_deletes_held"),
    ({"parts": {"vault": {"state": "failed", "note": "x"}}}, "standby_vault_failed"),
    ({"parts": {"identity": {"state": "failed", "note": "x"}}}, "standby_identity_failed"),
    ({"standby_version": "3.1.2", "code": {"state": "failed", "note": "prepare failed"}},
     "standby_version_mismatch"),
])
def test_high_findings(tmp_path, kw, code):
    _linked(tmp_path, **kw)
    assert _codes(dr_status.standby_status(tmp_path, NOW)).get(code) == "high"


def test_a_standby_following_an_update_is_only_medium(tmp_path):
    _linked(tmp_path, standby_version="3.1.2",
            code={"state": "ok", "attempt_at": (NOW - timedelta(minutes=5)).isoformat()})
    assert _codes(dr_status.standby_status(tmp_path, NOW)) == {"standby_updating": "medium"}


@pytest.mark.parametrize("code", [{"state": "ok"},       # never tried
                                  {"state": "ok", "attempt_at": "2026-10-22T10:00:00+00:00"}])
def test_a_standby_that_does_not_catch_up_becomes_high(tmp_path, code):
    _linked(tmp_path, standby_version="3.1.2", code=code)
    assert _codes(dr_status.standby_status(tmp_path, NOW)) == {"standby_version_mismatch": "high"}


def test_bundled_minio_documents_not_copied_is_said(tmp_path):
    _linked(tmp_path, parts={"docs": {"state": "not_copied",
                                      "note": "documents are in the bundled MinIO — NOT copied"}})
    assert _codes(dr_status.standby_status(tmp_path, NOW)) == {"standby_docs_not_copied": "medium"}


def test_after_a_switch_over(tmp_path):
    s = tmp_path / ".standby"
    s.mkdir()
    (s / "promoted.json").write_text(json.dumps({"stage": "documents", "old_main_diverged": {"velaris": "0/1"}}))
    (s / "fence.env").write_text("MAIN_HOST=old\n")
    codes = _codes(dr_status.standby_status(tmp_path, NOW))
    assert codes == {"standby_promote_unfinished": "high", "standby_old_main_diverged": "high",
                     "standby_old_main_not_fenced": "high"}


def test_a_demoted_server_says_it_must_not_run(tmp_path):
    s = tmp_path / ".standby"
    s.mkdir()
    (s / "main.json").write_text("{}")
    (s / "demoted.json").write_text(json.dumps({"demoted_at": "t"}))
    res = dr_status.standby_status(tmp_path, NOW)
    assert res["role"] == "demoted" and _codes(res) == {"standby_demoted_here": "high"}


def test_the_watcher_keeps_backup_and_standby_findings_apart(tmp_path):
    """One state file for both: a standby finding must not make the watcher
    forget a backup finding it already sent (or the reverse)."""
    f_backup = {"code": "backup_stale", "severity": "high", "message": "b"}
    f_standby = {"code": "standby_silent", "severity": "high", "message": "s"}
    due, state = due_notifications([f_backup, f_standby], {}, NOW)
    assert {f["code"] for f in due} == {"backup_stale", "standby_silent"}
    due, state = due_notifications([f_backup, f_standby], state, NOW + timedelta(minutes=5))
    assert due == []


async def test_standby_events_are_recorded_and_read_back(session):
    await dr.record_standby_event(session, event="link", peer="s2", detail={"copy_seconds": 13.5})
    await dr.record_standby_event(session, event="promote", peer="m1", detail={"main_reached": False})
    events = await dr.recent_standby_events(session)
    assert [e["event"] for e in events][:2] == ["promote", "link"] or \
        {e["event"] for e in events} >= {"promote", "link"}
    assert any(e["detail"].get("copy_seconds") == 13.5 for e in events)


async def test_an_unknown_event_is_refused(session):
    with pytest.raises(dr.DRError):
        await dr.record_standby_event(session, event="delete-everything")


def test_the_cli_never_fails_its_caller(monkeypatch):
    from case_service.compliance import standby_cli

    async def boom(args):
        raise RuntimeError("database down")
    monkeypatch.setattr(standby_cli, "_run", boom)
    assert standby_cli.main(["--event", "link", "--peer", "s2"]) == 0
    assert standby_cli.main(["--event", "link", "--strict"]) == 1


def test_diverged_writes_saved_on_rejoin_drop_to_medium(tmp_path):
    s = tmp_path / ".standby"
    s.mkdir()
    (s / "promoted.json").write_text(json.dumps({"stage": "done", "old_main_diverged": {"velaris": "0/9"},
                                                 "old_main_saved": {"host": "old", "path": "/p"}}))
    res = dr_status.standby_status(tmp_path, NOW)
    assert _codes(res) == {"standby_old_main_diverged": "medium"} and "/p" in res["findings"][0]["message"]


# ── automatic failover (step 12 d) ───────────────────────────────────────────

def _failover(tmp_path, *, agent_age=timedelta(seconds=3), role="main", agent=None, holding=True,
              lease=None, fenced=None, takeover=None):
    f = tmp_path / ".standby" / "failover"
    f.mkdir(parents=True, exist_ok=True)
    (f / "failover.json").write_text(json.dumps({"witness_target": "op@w", "node": "a", "term": 2,
                                                 "holding": holding, "lease": 30}))
    if agent_age is not None:
        (f / "agent.json").write_text(json.dumps({"at": (NOW - agent_age).isoformat(), "role": role,
                                                  **(agent or {})}))
    if lease is not None:
        (f / "lease.json").write_text(json.dumps(lease))
    if fenced is not None:
        (tmp_path / ".standby" / "fenced.json").write_text(json.dumps(fenced))
    if takeover is not None:
        (f / "takeover.json").write_text(json.dumps(takeover))


def test_failover_off_adds_nothing(tmp_path):
    _linked(tmp_path)
    res = dr_status.standby_status(tmp_path, NOW)
    assert "failover" not in res and not any(c.startswith("failover_") for c in _codes(res))


def test_failover_healthy_shows_facts_and_no_finding(tmp_path):
    _linked(tmp_path)
    _failover(tmp_path)
    res = dr_status.standby_status(tmp_path, NOW)
    assert res["failover"]["witness"] == "op@w" and res["failover"]["holding"] is True
    assert not any(c.startswith("failover_") for c in _codes(res))


def test_a_dead_agent_is_high(tmp_path):
    _linked(tmp_path)
    _failover(tmp_path, agent_age=timedelta(seconds=90))
    assert _codes(dr_status.standby_status(tmp_path, NOW))["failover_agent_down"] == "high"
    _failover(tmp_path, agent_age=None)
    (tmp_path / ".standby/failover/agent.json").unlink()
    assert _codes(dr_status.standby_status(tmp_path, NOW))["failover_agent_down"] == "high"


def test_a_self_stopped_main_is_high_even_with_failover_disabled(tmp_path):
    s = tmp_path / ".standby"
    s.mkdir()
    (s / "fenced.json").write_text(json.dumps({"fenced_at": "x", "reason": "no lease", "replaced": False}))
    assert _codes(dr_status.standby_status(tmp_path, NOW))["failover_fenced_here"] == "high"


def test_witness_unreachable_and_pause(tmp_path):
    _linked(tmp_path)
    _failover(tmp_path, agent={"witness_error": "timed out"},
              lease={"paused": {"at": (NOW - timedelta(hours=2)).isoformat(), "reason": "kernel"}})
    c = _codes(dr_status.standby_status(tmp_path, NOW))
    assert c["failover_witness_unreachable"] == "medium" and c["failover_paused"] == "medium"
    _failover(tmp_path, lease={"paused": {"at": (NOW - timedelta(hours=30)).isoformat()}})
    assert _codes(dr_status.standby_status(tmp_path, NOW))["failover_paused"] == "high"


def test_a_blocked_takeover_on_the_standby_is_high(tmp_path):
    _failover(tmp_path, role="standby", holding=False,
              agent={"takeover": "NOT taking over: the velaris replica is more than 10 s behind"})
    f = _codes(dr_status.standby_status(tmp_path, NOW))
    assert f["failover_takeover_blocked"] == "high"


def test_an_automatic_takeover_alerts_until_the_old_main_is_linked_again(tmp_path):
    _failover(tmp_path, takeover={"term": 2, "old_main": "m1", "finished_at": "t",
                                  "hook": {"ran": True, "exit": 0}})
    assert _codes(dr_status.standby_status(tmp_path, NOW))["failover_promoted_automatically"] == "high"
    _linked(tmp_path)                                   # the old main is this server's standby now
    assert "failover_promoted_automatically" not in _codes(dr_status.standby_status(tmp_path, NOW))
    _failover(tmp_path, takeover={"term": 2, "promote_error": "Refused: jq missing"})
    assert _codes(dr_status.standby_status(tmp_path, NOW))["failover_promote_failed"] == "high"


def test_failover_alerts_carry_their_own_title():
    from case_service.compliance.dr_watch import _TITLE, _kind
    assert _TITLE[_kind("failover_agent_down")] == "Automatic failover needs attention"
    assert _TITLE[_kind("standby_silent")] == "Standby server needs attention"
    assert _TITLE[_kind("backup_overdue")] == "Backup needs attention"


def test_malformed_marks_never_take_the_posture_down(tmp_path):
    """A hand-edited or odd mark (not an object, wrong types) is read as absent or
    shown as text — it must never turn the DR posture into a 500."""
    _failover(tmp_path, lease={"paused": {"at": None, "reason": 7}},
              takeover={"term": 2, "old_main": "m1", "finished_at": "t", "hook": "x"})
    c = _codes(dr_status.standby_status(tmp_path, NOW))
    assert c["failover_paused"] == "medium" and c["failover_promoted_automatically"] == "high"
    for name in ("failover/failover.json", "failover/agent.json", "failover/lease.json",
                 "failover/takeover.json", "fenced.json", "main.json", "status.json"):
        (tmp_path / ".standby" / name).write_text("[]")
        dr_status.standby_status(tmp_path, NOW)
