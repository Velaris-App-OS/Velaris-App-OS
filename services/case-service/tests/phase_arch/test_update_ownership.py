"""R6 2026-11-06 — a tarball update must not carry the packager's folders or owner.

The 3.1.17 package carried the packaging machine's own 0700
`deploy/docker-compose/pooler-socket` folder; the updater (as root) applied its
owner and mode to vx-load, the services lost the database pooler and the
platform stopped. The package now holds files only, and the updater keeps
existing folders and gives the shipped files to the install's owner (that part
needs root and was proven on vx-load: 3.1.18 old updater, 3.1.19 new updater).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
UPD = (ROOT / "update-velaris.sh").read_text()


def test_package_holds_no_folder_entries(tmp_path):
    subprocess.run(["bash", str(ROOT / "scripts/package-release.sh"),
                    "--version", "0.0.1", "--out", str(tmp_path)],
                   check=True, capture_output=True)
    with tarfile.open(tmp_path / "v0.0.1.tar.gz") as tar:
        members = tar.getmembers()
    assert members, "empty package"
    assert not [m.name for m in members if m.isdir()]
    names = {m.name for m in members}
    # The folder still arrives on a fresh install, through its file.
    assert "velaris/deploy/docker-compose/pooler-socket/README.md" in names
    assert "velaris/.release-files" in names


def test_update_keeps_existing_folders_and_takes_no_owner_from_the_archive():
    line = next(l for l in UPD.splitlines()
                if 'tar -xzf "$VELARIS_SCRATCH/velaris-update.tar.gz"' in l)
    i = UPD.splitlines().index(line)
    cmd = " ".join(UPD.splitlines()[i:i + 2])
    assert "--no-same-owner" in cmd and "--no-overwrite-dir" in cmd


def test_update_gives_shipped_files_to_the_install_owner_only_as_root():
    assert "_VD_OWNER=\"$(stat -c '%u:%g' \"$VELARIS_DIR\"" in UPD
    assert '[[ "$(id -u)" == 0 && "${_VD_OWNER%%:*}" != 0 ]]' in UPD
    # Never a path that leaves the install.
    assert "grep -v -e '^/' -e '\\.\\.' \"$_NEW_FILES\"" in UPD
