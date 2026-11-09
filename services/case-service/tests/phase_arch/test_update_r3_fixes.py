"""R3 upgrade rehearsal (2026-11-01) — the update/rollback defects stay fixed.

Each test names the finding it guards (docs/Future/Nov_TODO.md R3). The two
pieces of real logic — carrying velaris.yaml settings over and removing files a
release no longer ships — are extracted from update-velaris.sh and RUN; the
rest are placement checks on the scripts.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
UPD = (ROOT / "update-velaris.sh").read_text()
REFRESH = (ROOT / "deploy/systemd/refresh-after-update.sh").read_text()


def _yaml_merge_py() -> str:
    m = re.search(r"python3 - \"\$_YAML_KEPT\" \"\$VELARIS_DIR/velaris.yaml\" <<'PY' \\\n.*?\n(.*?)\nPY\n", UPD, re.S)
    assert m, "velaris.yaml merge block not found"
    return m.group(1)


def _merge(tmp_path, old: str, new: str) -> str:
    (tmp_path / "m.py").write_text(_yaml_merge_py())
    (tmp_path / "old.yaml").write_text(old)
    (tmp_path / "new.yaml").write_text(new)
    subprocess.run(["python3", str(tmp_path / "m.py"), str(tmp_path / "old.yaml"), str(tmp_path / "new.yaml")], check=True)
    return (tmp_path / "new.yaml").read_text()


NEW = """velaris:
  version: "3.1.3"
  instance: "my-velaris"
  database: postgresql
  events: kafka
  updates:
    source: "github"                 # "github" or "server"
    server_url: "https://updates.velaris.io/latest"
    channel: "stable"
    auto_update: false
    new_knob: true
"""


def test_f1_operator_settings_survive_a_tarball_update(tmp_path):
    old = """velaris:
  version: "3.1.2"
  instance: "acme-prod"
  database: mysql
  db_host: "db.internal"
  db_port: 3306
  db_name: "velaris"
  db_user: "velaris"
  events: redpanda
  updates:
    source: "server"                 # "github" or "server"
    server_url: "http://10.0.0.5:8799/latest"
    channel: "beta"
    auto_update: true
"""
    out = _merge(tmp_path, old, NEW)
    assert 'instance: "acme-prod"' in out and "database: mysql" in out
    assert 'db_host: "db.internal"' in out and "db_port: 3306" in out           # setup's lines carried over
    assert 'source: "server"                 # "github" or "server"' in out    # comment kept
    assert 'server_url: "http://10.0.0.5:8799/latest"' in out and 'channel: "beta"' in out
    assert "auto_update: true" in out and "new_knob: true" in out               # new key keeps the default
    assert "events: kafka" in out                                               # a shipped value the release changed
    assert 'version: "3.1.3"' in out                                            # the updater's own
    assert _merge(tmp_path, old, out) == out                                    # re-run changes nothing


def test_f1_an_unchanged_install_gets_the_shipped_file(tmp_path):
    assert _merge(tmp_path, NEW.replace("3.1.3", "3.1.2"), NEW) == NEW


def _release(tmp_path, name: str, files: dict[str, str]) -> Path:
    root = tmp_path / name / "velaris"
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    (root / ".release-files").write_text("\n".join(sorted([*files, ".release-files"])) + "\n")
    tgz = tmp_path / f"{name}.tgz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(root, arcname="velaris")
    return tgz


def test_f11_files_the_target_does_not_ship_are_removed_and_nothing_else(tmp_path):
    newer = _release(tmp_path, "v2", {"velaris.yaml": "velaris:\n  version: \"2\"\n", "services/keep/a.py": "a",
                                      "services/gone/pkg/g.py": "g", "only-v2.txt": "x"})
    older = _release(tmp_path, "v1", {"velaris.yaml": "velaris:\n  version: \"1\"\n", "services/keep/a.py": "a"})
    inst = tmp_path / "inst"
    inst.mkdir()
    subprocess.run(["tar", "-xzf", str(newer), "-C", str(inst), "--strip-components=1"], check=True)
    (inst / ".env").write_text("SECRET=1\n")                                     # the install's own
    (inst / "services/gone/operator-note.txt").write_text("mine")              # an operator's file
    block = UPD[UPD.index('  _YAML_KEPT="$VELARIS_SCRATCH/velaris.yaml.before-update"'):
                UPD.index('  cp "$_NEW_FILES" "$VELARIS_DIR/.release-files"')]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = ('green(){ echo "$*"; }; yellow(){ echo "$*"; }\n'
              f'VELARIS_DIR="{inst}"; VELARIS_SCRATCH="{scratch}"; LATEST_VERSION=1\n'
              f'cp "{older}" "$VELARIS_SCRATCH/velaris-update.tar.gz"\n' + block)
    subprocess.run(["bash", "-c", script], check=True)
    left = sorted(str(p.relative_to(inst)) for p in inst.rglob("*") if p.is_file())
    assert left == [".env", ".release-files", "services/gone/operator-note.txt", "services/keep/a.py", "velaris.yaml"]


def test_f12_proxies_are_recreated_after_every_code_change():
    i = UPD.index("_proxy_svcs=\"\"")
    assert UPD.index('bash "$VELARIS_DIR/start-velaris.sh" 9<&-') < i < UPD.index('step "Post-update health check..."')
    assert "up -d --no-deps --force-recreate $_proxy_svcs" in UPD


def test_f12_gate_checks_routes_and_only_the_services_the_release_routes():
    assert "_routes_probe" in UPD and "000|502|503|504)" in UPD
    for svc in ("analytics", "hxgraph", "hxmigrate", "commerce", "portal", "user"):
        assert f"_routed {svc}_service" in UPD


def test_f9_gateway_probe_is_plain_http():
    assert '_gateway_probe() { curl -sf --max-time 3 "http://localhost:${SERVICE_PORT}/health"' in UPD
    assert '_velaris_probe "${SERVICE_PORT}"' not in UPD and "${SERVICE_PORT}:gateway" not in UPD


def test_f10_a_stopped_update_is_finished_whatever_the_channel_pins():
    i = UPD.index('if [[ "$RESUMING" == false && "$LATEST_VERSION" != "$CURRENT_VERSION" ]]')
    refuse = UPD.index('but v$CURRENT_VERSION is installed — not downgrading"')
    assert i < refuse
    assert 'LATEST_VERSION="$CURRENT_VERSION"' in UPD[i:refuse]


def test_f2_only_keys_this_run_added_are_generated():
    assert '[[ "$ENV_ADDED_KEYS" == *" ${key} "* ]] || return 0' in UPD
    assert UPD.index('ENV_ADDED_KEYS="${ENV_ADDED_KEYS}${key} "') < UPD.index("_gen_if_empty VELARIS_CASE_STORAGE_MASTER_KEY")


def test_f7_secrets_are_made_after_openbao_is_unsealed():
    assert REFRESH.index('green "  ✓ OpenBao unsealed"') < REFRESH.index("velaris_ensure_internal_secret")
    assert REFRESH.index('green "  ✓ OpenBao unsealed"') < REFRESH.index("velaris_backup.py\" ensure-key")


def test_f8_certs_dir_exists_before_compose_up():
    for f, up in (("setup-velaris.sh", "DB_UP_LOG=$(mktemp)"),
                  ("start-velaris.sh", 'docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d $_UP_SVCS'),
                  ("deploy/systemd/refresh-after-update.sh", "_compose up -d $_UP_SVCS")):
        text = (ROOT / f).read_text()
        assert text.index("velaris_tls_certs_dir") < text.index(up), f


def test_f8_certs_dir_helper(tmp_path):
    subprocess.run(["bash", "-c", f'. "{ROOT}/deploy/tls/certs-dir.sh"; velaris_tls_certs_dir "{tmp_path}"'], check=True)
    d = tmp_path / "deploy/tls/certs"
    assert d.is_dir() and oct(d.stat().st_mode)[-3:] == "700"


def test_f13_mysql_family_is_updated_not_refused():
    assert "supports Postgres installs only" not in UPD
    assert "mysql|mariadb) DB_FAMILY=mysql" in UPD
    assert '_db_dump() { velaris_mysqldump; }' in UPD
    assert 'find "$VELARIS_DIR/migrations/mysql"' in UPD
    assert "apply releases/manifest.mysql.sql by hand" not in REFRESH.split("elif [ -f")[0]
    ext = (ROOT / "deploy/external-services.sh").read_text()
    assert "velaris_mysql()" in ext and "velaris_mysqldump()" in ext and "--column-statistics=0" in ext


def test_f11_a_hostile_list_cannot_reach_outside_or_install_files(tmp_path):
    """A list naming a path through a symlinked directory, or `.env`, removes neither."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("keep me")
    older = _release(tmp_path, "v1", {"velaris.yaml": "velaris:\n  version: \"1\"\n"})
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "velaris.yaml").write_text("velaris:\n  version: \"2\"\n")
    (inst / "link").symlink_to(outside)
    (inst / ".env").write_text("SECRET=1\n")
    (inst / ".release-files").write_text("velaris.yaml\nlink/victim.txt\n.env\n.release-files\n")
    block = UPD[UPD.index('  _YAML_KEPT="$VELARIS_SCRATCH/velaris.yaml.before-update"'):
                UPD.index('  cp "$_NEW_FILES" "$VELARIS_DIR/.release-files"')]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = ('green(){ echo "$*"; }; yellow(){ echo "$*"; }\n'
              f'VELARIS_DIR="{inst}"; VELARIS_SCRATCH="{scratch}"; LATEST_VERSION=1\n'
              f'cp "{older}" "$VELARIS_SCRATCH/velaris-update.tar.gz"\n' + block)
    subprocess.run(["bash", "-c", script], check=True)
    assert (outside / "victim.txt").read_text() == "keep me"
    assert (inst / ".env").read_text() == "SECRET=1\n"


def test_no_database_password_on_a_command_line():
    """`-e MYSQL_PWD=value` / `env MYSQL_PWD=…` put the password in the process list."""
    for f in ("update-velaris.sh", "setup-velaris.sh", "start-velaris.sh", "deploy/external-services.sh",
              "K8/MySQL/_common.sh", "K8/MariaDB/_common.sh"):
        code = "\n".join(l for l in (ROOT / f).read_text().splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r'-e MYSQL_PWD=|env MYSQL_PWD=|MYSQL_PWD=\'\$\(db_password\)\'', code), f
