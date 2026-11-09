"""The launch script must not exhaust the machine it starts on.

**What happened, 2026-08-24.** `uvicorn --reload` watches the working directory
when it is not told otherwise — the whole repository, `.venv`, `node_modules`,
`Velaris/` and `dist` included: about 18,600 inotify watches per process. That
was survivable while the platform was one process. #13b-e made it four, which
came to 65,399 of the kernel's default 65,536 — so Studio's vite server, the
next thing `start-velaris.sh` launches, died with `ENOSPC` at boot and the UI
was simply gone. Every API answered 200 throughout, which is why nothing looked
wrong from the server side.

The fix is one flag per service. This test exists because that flag is easy to
drop when a service is added or a launch line is edited, and the symptom lands
on a different program entirely.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "start-velaris.sh"


def _launch_commands() -> list[str]:
    """Every `uvicorn` invocation in the script, line continuations joined."""
    text = SCRIPT.read_text()
    joined = re.sub(r"\\\n\s*", " ", text)
    return [ln.strip() for ln in joined.splitlines()
            if "uvicorn" in ln and not ln.strip().startswith("#")]


def test_the_script_exists():
    assert SCRIPT.is_file(), f"{SCRIPT} is the supported way to start the platform"


#: The port is configuration now (see test_port_configuration.py), so a launch
#: is identified by the app it runs, not by a number that may legitimately move.
SERVICES = {
    "engine": "helix_engine.main:app",
    "case-service": "case_service.main:app",
    "analytics-service": "analytics_service.main:app",
    "hxgraph-service": "hxgraph_service.main:app",
    "hxmigrate-service": "hxmigrate_service.main:app",
    "commerce-service": "commerce_service.main:app",
    "portal-service": "portal_service.main:app",
    "user-service": "user_service.main:app",
    "worker-service": "case_service.worker.main:app",
}


def test_every_service_is_launched_by_the_script():
    """A service nobody starts is a service that is down after every reboot."""
    commands = " ".join(_launch_commands())
    for name, app in SERVICES.items():
        assert app in commands, (
            f"no uvicorn launch for {name} ({app}) in start-velaris.sh")


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_reloading_services_bound_their_watchers(name):
    """`--reload` without `--reload-dir` watches the entire repository.

    Four of those exhaust inotify and take down whatever starts next — which
    is Studio, not an API, so the failure appears somewhere nobody is looking.
    """
    app = SERVICES[name]
    cmd = next((c for c in _launch_commands() if app in c), None)
    assert cmd is not None, f"no uvicorn launch for {name}"
    if "--reload" not in cmd:
        pytest.skip(f"{name} does not use --reload")
    assert "--reload-dir" in cmd, (
        f"the launch for {name} uses --reload with no --reload-dir, so it "
        "watches the whole repo (~18,600 inotify watches). With four services "
        "that exhausts the kernel default and Studio's vite server dies with "
        "ENOSPC while every API still answers 200."
    )


def test_the_shared_package_stays_watched():
    """Bounding the watchers must not cost the reload that made them useful.

    A service that watches only its own directory silently stops reloading on
    `velaris_shared` edits — the package every one of them is built on.
    """
    offenders = [
        name for name, app in SERVICES.items()
        for c in _launch_commands()
        if app in c and "--reload-dir" in c and "services/" in c
        and "services/_shared" not in c
    ]
    assert not offenders, (
        f"{offenders} bound their reload watchers but left out services/_shared, "
        "so edits to the shared package will not reload them.")
