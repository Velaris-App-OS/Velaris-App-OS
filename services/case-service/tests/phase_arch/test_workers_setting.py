"""VELARIS_WORKERS_<SERVICE> — processes per service (R6, 2026-10-30).

Each service was one process, so it could use one CPU core however big the box.
Unset, empty or 1 must leave the command line exactly as it was; a typo must
never stop a service from starting."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
HELPER = ROOT / "deploy/workers-args.sh"


def _args(service: str, **env: str) -> tuple[str, str]:
    e = {k: v for k, v in os.environ.items() if not k.startswith("VELARIS_WORKERS_")}
    e.pop("VELARIS_RUN_MODE", None)
    e["VELARIS_CASE_REDIS_ENABLED"] = "true"
    e.update(env)
    r = subprocess.run(["bash", "-c", f'. "{HELPER}"; _velaris_workers_args {service}'],
                       env=e, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    return r.stdout, r.stderr


@pytest.mark.parametrize("value", [None, "", "  ", "1"])
def test_unset_empty_or_one_changes_nothing(value):
    env = {} if value is None else {"VELARIS_WORKERS_CASE_SERVICE": value}
    assert _args("case-service", **env) == ("", "")


def test_a_number_becomes_the_workers_flag_for_that_service_only():
    assert _args("case-service", VELARIS_WORKERS_CASE_SERVICE="4")[0] == "--workers 4"
    assert _args("user-service", VELARIS_WORKERS_CASE_SERVICE="4")[0] == ""
    assert _args("user-service", VELARIS_WORKERS_USER_SERVICE="3")[0] == "--workers 3"


@pytest.mark.parametrize("value", ["four", "-2", "0", "04", "2.5", "65", "4 4"])
def test_a_bad_value_is_ignored_with_a_line_saying_so(value):
    out, err = _args("case-service", VELARIS_WORKERS_CASE_SERVICE=value)
    assert out == "" and "ignored, one process" in err


@pytest.mark.parametrize("cache", ["", "false", "0"])
def test_more_than_one_process_needs_the_shared_cache(cache):
    # Codes and attempt limits live in the cache; without it N processes were
    # N times every limit and a code from one process unknown to the next.
    out, err = _args("case-service", VELARIS_WORKERS_CASE_SERVICE="4", VELARIS_CASE_REDIS_ENABLED=cache)
    assert out == "" and "VELARIS_CASE_REDIS_ENABLED=true" in err
    assert _args("case-service", VELARIS_WORKERS_CASE_SERVICE="4", VELARIS_CASE_REDIS_ENABLED="True")[0] == "--workers 4"


def test_never_in_development_where_reload_is_on():
    assert _args("case-service", VELARIS_WORKERS_CASE_SERVICE="4", VELARIS_RUN_MODE="development")[0] == ""


def test_both_launch_paths_use_it():
    wrapper = (ROOT / "deploy/systemd/velaris-exec.sh").read_text()
    assert 'deploy/workers-args.sh' in wrapper and 'exec "$@" $_workers_args' in wrapper
    start = (ROOT / "start-velaris.sh").read_text()
    for svc in ("case-service", "analytics-service", "hxgraph-service", "hxmigrate-service",
                "commerce-service", "portal-service", "user-service", "worker-service"):
        assert f"$(_velaris_workers_args {svc})" in start, svc
