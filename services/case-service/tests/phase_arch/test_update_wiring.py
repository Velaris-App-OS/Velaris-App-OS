"""What an install gets wired up when it is updated or handed a new unit.

Three defects found on clean machines on 2026-10-20 (Nov_TODO, R5e step 7 part e),
each one invisible on this dev box:

1. **Every fresh install skipped the update agent.** setup re-runs itself under
   `sg docker`; the kernel then hides our own units' sockets from `ss -p` (the gid
   differs), so the port preflight called every Velaris port "owner unknown" and
   `--install-timer` refused. The owner check now falls back to the socket's
   cgroup, which needs no such access.
2. **An update FROM v3.1.2 generated neither the internal service secret nor the
   backup key.** It runs the old release's updater, whose only call into the new
   tree is `deploy/systemd/refresh-after-update.sh` — so that hook must do both.
3. **`install.sh` left commerce down.** It restarted case-service (which then
   stops running commerce beside it) but only enabled the new unit.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
PORTS_SH = ROOT / "deploy" / "ports" / "velaris-ports.sh"
REFRESH = ROOT / "deploy" / "systemd" / "refresh-after-update.sh"
INSTALL = ROOT / "deploy" / "systemd" / "install.sh"

_LISTEN = "LISTEN 0 2048 127.0.0.1:9999 0.0.0.0:*"


def _owner(tmp_path: pathlib.Path, cgroup_line: str) -> str:
    """Run `_velaris_port_owner 9999` against a stub `ss` that hides the pid."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ss").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *--cgroup*) echo "{_LISTEN} {cgroup_line}" ;;\n'
        f'  *) echo "{_LISTEN}" ;;\n'   # -ltn and -ltnp alike: no users:(pid=…)
        "esac\n"
    )
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run(
        ["bash", "-c", f'. "{PORTS_SH}"; _velaris_port_owner 9999'],
        env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_a_hidden_pid_in_a_velaris_unit_is_ours(tmp_path):
    line = "cgroup:/system.slice/velaris-commerce-service.service"
    assert _owner(tmp_path, line) == "velaris"


def test_a_hidden_pid_anywhere_else_is_still_unknown(tmp_path):
    """The fallback must not turn a stranger's port into ours."""
    assert _owner(tmp_path, "cgroup:/user.slice/user-1002.slice/session-3.scope") == "unknown"


def test_an_ss_without_cgroup_support_behaves_as_before(tmp_path):
    assert _owner(tmp_path, "") == "unknown"


@pytest.mark.parametrize("needs", [
    "velaris_ensure_internal_secret",
    "velaris_backup.py\" ensure-key",
])
def test_the_refresh_hook_generates_what_an_older_updater_does_not(needs):
    text = REFRESH.read_text()
    assert needs in text, f"refresh-after-update.sh no longer runs {needs}"
    # …and before the units are restarted, or the services start without it.
    assert text.index(needs) < text.index("systemctl restart"), needs


def test_the_handover_starts_the_commerce_and_portal_units():
    text = INSTALL.read_text()
    branch = re.search(r"beside it.*?\nfi\n", text, re.S)
    assert branch, "install.sh no longer hands the companions over from case-service"
    body = branch.group(0)
    assert 'systemctl start "$_u"' in body, (
        "install.sh restarts case-service (which stops running its companions) "
        "without starting their own units — they are left down"
    )
    for pair in ("commerce_service:velaris-commerce-service",
                 "portal_service:velaris-portal-service",
                 "user_service:velaris-user-service"):
        assert pair in text, f"install.sh does not hand over {pair}"


def test_case_service_runs_both_companions_until_their_units_exist():
    text = (ROOT / "deploy/systemd/velaris-exec.sh").read_text()
    # The port is checked (R1): a typo is the default, never `--port 82O7`.
    for call in ('_velaris_companion commerce-service commerce_service "$(_velaris_port VELARIS_COMMERCE_PORT 8207)"',
                 '_velaris_companion portal-service portal_service "$(_velaris_port VELARIS_PORTAL_PORT 8205)"',
                 '_velaris_companion user-service user_service "$(_velaris_port VELARIS_USER_PORT 8206)"'):
        assert call in text, call
    # Only for case-service's own unit, and never once the unit is installed.
    assert '[ -f "$_unit_dir/velaris-$_c_svc.service" ] && return 0' in text


def test_setup_retries_the_containers_and_prints_a_command_that_works():
    """2026-10-22 clean box: setup's `compose up` failed on the network and the
    fallback it printed had no profiles — following it never started the pooler,
    so every unit waited on it for ever — and its other advice (start-velaris.sh)
    collides with the units it had just installed."""
    text = (ROOT / "setup-velaris.sh").read_text()
    block = text[text.index("_UP_SVCS=\"\" _UP_OK=1"):text.index("SYSTEMD_MANAGED=1")]
    assert "for _try in 1 2 3; do" in block
    hint = block[block.index('warn "Containers not all started'):]
    assert 'COMPOSE_PROFILES=\\"${COMPOSE_PROFILES:-}\\" docker compose' in hint
    assert "up -d $_UP_SVCS" in hint
    assert "start-velaris.sh" not in hint.split("sudo systemctl start")[0]
