"""verify_restore.py `vault_keys` — R5b drill 2026-10-30: with the KEK lost every
encrypted field was unreadable and `restore-velaris.sh --verify` still passed."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

_V = Path(__file__).resolve().parents[4] / "scripts" / "backup" / "verify_restore.py"


@pytest.fixture()
def vr(monkeypatch):
    spec = importlib.util.spec_from_file_location("verify_restore_under_test", _V)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _with(vr, monkeypatch, active, unwrapped):
    async def state(_s):
        return {"active": active, "unwrapped": unwrapped}
    monkeypatch.setattr(vr, "_vault_state", state)


def test_keys_that_opened_at_backup_time_must_open_now(vr, monkeypatch):
    m = {"probe": {"vault": {"active": 2, "unwrapped": 2}}}
    _with(vr, monkeypatch, 2, 0)                          # the KEK in .env is another one
    ok, detail = asyncio.run(vr._vault(None, m))
    assert ok is False and "0 of 2" in detail
    _with(vr, monkeypatch, 2, 2)
    assert asyncio.run(vr._vault(None, m))[0] is True


def test_without_a_probe_every_active_key_must_open(vr, monkeypatch):
    _with(vr, monkeypatch, 3, 2)
    assert asyncio.run(vr._vault(None, {}))[0] is False
    _with(vr, monkeypatch, 0, 0)                          # no encrypted fields yet
    assert asyncio.run(vr._vault(None, {})) == (True, "no tenant keys (no encrypted fields yet)")


def test_the_check_runs_in_verify_and_is_recorded_by_the_probe():
    src = _V.read_text()
    assert '("vault_keys", lambda s: _vault(s, manifest))' in src
    assert '"vault": vault' in src
