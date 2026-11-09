"""The one-shot OpenBao agent retries a refused or missing AppRole sign-in
forever, so `secrets-render.sh` — run by start and by `restore --verify` — hung
with no end (R5b re-run, 2026-10-30). It now refuses at once when a sign-in
file is missing and gives up after a bounded wait, leaving .env untouched."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _box(tmp_path: Path, creds: bool) -> tuple[Path, dict]:
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/secrets-render.sh", tmp_path / "scripts/secrets-render.sh")
    agent = tmp_path / "deploy/openbao/agent"
    agent.mkdir(parents=True)
    if creds:
        (agent / "role_id").write_text("r")
        (agent / "secret_id").write_text("s")
    (tmp_path / ".env").write_text("VELARIS_CASE_DATABASE_URL=x\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "curl").write_text("#!/bin/sh\nprintf 200\n")
    (bin_ / "docker").write_text("#!/bin/sh\n[ \"$1\" = run ] && exec sleep 30\nexit 0\n")
    for f in ("curl", "docker"):
        (bin_ / f).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "VELARIS_SECRETS_RENDER_TIMEOUT": "1"}
    return tmp_path, env


def test_a_missing_sign_in_file_is_refused_at_once(tmp_path):
    box, env = _box(tmp_path, creds=False)
    r = subprocess.run(["bash", str(box / "scripts/secrets-render.sh"), "--check"], env=env,
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and "sign-in file missing" in r.stdout


def test_an_agent_that_never_finishes_is_given_up_on(tmp_path):
    box, env = _box(tmp_path, creds=True)
    t = time.monotonic()
    r = subprocess.run(["bash", str(box / "scripts/secrets-render.sh")], env=env,
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "did not render within 1s" in r.stdout
    assert time.monotonic() - t < 10
    assert (box / ".env").read_text() == "VELARIS_CASE_DATABASE_URL=x\n"
