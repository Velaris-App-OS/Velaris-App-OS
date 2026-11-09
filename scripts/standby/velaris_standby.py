#!/usr/bin/env python3
"""Velaris standby server — link, status, unlink (R5c, Nov_TODO step 11).

A standby is a second server that receives every database change as it happens,
so that if the main server is lost the standby can be promoted with (almost)
nothing lost. It is an OPTION: nothing here runs, and nothing on the main server
changes, until an operator runs `standby-velaris.sh link`.

How the two servers are joined (decided 2026-10-22):

* **SSH only — no new open port.** The main's `replication-gw` container
  (compose profile `standby`) listens on the main's loopback for velaris-db and
  temporal-db. The standby reaches it through an SSH tunnel
  (velaris-standby-tunnel.service) whose key may forward to those two ports and
  nothing else. Neither database container on the main is recreated.
* **Both databases stream** (PostgreSQL physical replication, asynchronous).
  Temporal's too: without it, open cases' timers stop after a switch-over.
* **The main's disk comes first.** Each database keeps WAL for the standby in a
  replication slot, capped by `max_slot_wal_keep_size`
  (VELARIS_STANDBY_MAX_WAL_KEEP, default 10GB). A standby that falls further
  behind is marked "needs re-link"; the main never fills its disk for it.
* On the standby the replicas run as two containers on the host network, bound
  to 127.0.0.1, on the SAME volumes the standby's compose file names — so a
  promote (step 11 part c) hands the data to the ordinary compose services.

* **Everything else the standby needs, pulled every 60 s** (part b,
  velaris-standby-sync.timer on the standby): the documents (rsync), the main's
  `.env`, and — when OpenBao holds the secrets — its raft snapshot and init
  files, fetched only when they change. The same key carries it: on the main it
  is bound to `standby-velaris.sh serve`, which answers a fixed list of requests
  and nothing else. Ollama's models are copied once, at link.

Commands (main): link HOST, status, unlink; `serve` is the main's side of the
standby's key. (standby): `sync`, run by its timer. The `remote-*` commands are
what the main runs ON the standby, over the operator's SSH session.

Standard library only: on the standby this runs before any venv exists.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
import re
import shutil
import secrets
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy" / "docker-compose" / "docker-compose.yml"
STATE = ROOT / ".standby"
MAIN_RECORD = STATE / "main.json"
STATUS_FILE = STATE / "status.json"
TUNNEL_ENV = STATE / "tunnel.env"
SYNC_REPORT = STATE / "sync_report.json"     # main: what the standby last reported
DEMOTED = STATE / "demoted.json"             # main: replaced by its standby — never start again
PROMOTED = STATE / "promoted.json"           # standby → main: what the switch-over did
FENCE_ENV = STATE / "fence.env"              # new main: an old main to demote when it reappears
PREPARED = STATE / "prepared.json"           # standby: dependencies + images in place
SYNC_STATE = STATE / "sync.json"             # standby: what it holds and when
MAIN_ENV_COPY = STATE / "main.env"           # standby: the main's .env (promote installs it)
VAULT_COPY = STATE / "vault"                 # standby: raft snapshot + init files
OPENBAO_DIR = ROOT / "deploy" / "openbao"
BAO_FILES = (".bao-init.json", "agent/role_id", "agent/secret_id")
# rsync's own server command for a pull with the options `sync` uses (seen
# 2026-10-22, rsync 3.2.7: delete options are the receiver's and never sent).
# Exactly our client's short options (-rtp: regular files, times, modes — no
# links, devices or owners) and rsync's own "e.<caps>", rooted at one folder.
FOLDERS = ("docs", "recordings", "deletions")
# The deletion ledger (purged accounts' signed records) is APPEND-ONLY: copied,
# never mirrored with deletions — a record missing on the main is no reason to
# drop the standby's copy, which a promote makes the new main's ledger.
APPEND_ONLY = {"deletions"}
RSYNC_SENDER_RE = re.compile(r"rsync --server --sender -[rtp]+e\.[A-Za-z]+ \. (docs|recordings|deletions)/")
MAIN_CONFIG_COPY_NAME = "main.velaris.yaml"
# This install's identity outside .env: setup's marker (the start gate refuses
# without it — a promoted standby would not have started) and the product-key
# cache. Copied every minute, installed by promote.
IDENTITY_FILES = (".velaris-setup-complete", ".velaris-key")
IDENTITY_COPY = STATE / "identity"
# serve-reads (R5d 11b part c): the main's reads go to the standby's replica
# through a main-side tunnel (velaris-standby-reads.service).
READS_ENV = STATE / "reads-tunnel.env"         # main: the tunnel's settings (its path unit starts it)
READS_KEY = STATE / "reads_key"                # main: may forward to the standby's replica port only
READS_KNOWN = STATE / "reads_known_hosts"      # main: the standby's host keys, copied at `on`
READS_KEY_MARK = "velaris-standby-reads"
# The tunnel's local end is a UNIX socket in the 0700 .standby folder, not a
# loopback port: a port is free for a few seconds each time the tunnel
# reconnects, and any local user who bound it then could pose as PostgreSQL,
# ask for a cleartext password and get the platform's database password
# (asyncpg answers that request). Only the install's owner can reach — or
# take — a socket in a folder only it can enter (11b (c) audit, 2026-10-23).
READS_SOCK_DIR = STATE / "reads-sock"
READS_SOCK = READS_SOCK_DIR / ".s.PGSQL.5432"   # the name PostgreSQL clients look for
READS_UNIT = Path("/etc/systemd/system/velaris-standby-reads.path")
READ_URL_KEY = "VELARIS_CASE_DATABASE_READ_URL"

ROLE = "velaris_replicator"
SLOT = "velaris_standby"
APP_NAME = "velaris_standby"
HBA_MARK = "# velaris standby (R5c) — managed by standby-velaris.sh"
HBA_LINE = f"host replication {ROLE} samenet scram-sha-256"
KEY_MARK = "velaris-standby-tunnel"
STALL_SECONDS = 30   # the standby reports every 10 s (wal_receiver_status_interval)
PG_UID = "70"   # the postgres user in the official alpine image
# The replicas run on the standby's HOST network, so "127.0.0.1" there is the
# whole server's loopback — not a container's, as on the main. The pg_hba.conf
# a base backup copies from the main trusts 127.0.0.1 (the image's default):
# any local user on the standby got a superuser session with no password
# (proven 2026-10-22). The replicas use this file instead.
REPLICA_HBA_NAME = "pg_hba.standby.conf"
REPLICA_HBA = ("# velaris standby replica (R5c) — host network: no trust over TCP.\n"
               "local all all trust\n"
               "host all all 127.0.0.1/32 scram-sha-256\n")
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}|\[?[0-9A-Fa-f:.]+\]?")
USER_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")

# key → (compose service, superuser, database, main gateway port setting + default,
#        replica container on the standby, its loopback port setting + default)
DBS = {
    "velaris": ("velaris-db", "velaris", "velaris",
                ("VELARIS_REPLICATION_PORT", "5442"),
                "velaris-standby-db", ("VELARIS_STANDBY_DB_PORT", "5432")),
    "temporal": ("temporal-db", "temporal", "temporal",
                 ("VELARIS_REPLICATION_TEMPORAL_PORT", "5443"),
                 "velaris-standby-temporal-db", ("VELARIS_STANDBY_TEMPORAL_DB_PORT", "5444")),
}


class Refused(Exception):
    """A precondition failed; the message says what to do."""


def say(msg: str) -> None:
    print(msg, flush=True)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(errors="replace").splitlines():
            m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[m.group(1)] = v
    return out


ENV = read_env(ROOT / ".env")


def setting(name: str, default: str) -> str:
    value = os.environ.get(name) or ENV.get(name) or default
    if name.endswith("_PORT"):
        # R1: a mistyped port is the default, with a line saying so — it is
        # written into compose and into authorized_keys' permitopen.
        v = value.strip()
        if not (v.isdigit() and not v.startswith("0") and int(v) <= 65535):
            print(f"{name}={value!r} is not a port — using {default}", file=sys.stderr)
            return default
        return v
    return value


def yaml_get(key: str) -> str:
    p = ROOT / "velaris.yaml"
    if p.is_file():
        for line in p.read_text(errors="replace").splitlines():
            m = re.match(rf"^\s*{re.escape(key)}:\s*[\"']?([^\"'#]*)[\"']?", line)
            if m:
                return m.group(1).strip()
    return ""


def version() -> str:
    return yaml_get("version") or "0.0.0"


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True,
        env: dict | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} … failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def private_dir() -> None:
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE, 0o700)


def write_private(path: Path, text: str) -> None:
    private_dir()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def project() -> str:
    """The compose project name (volume prefix). Same rule as compose itself."""
    return setting("COMPOSE_PROJECT_NAME", COMPOSE_FILE.parent.name)


def db_image() -> str:
    """velaris-db's image, read from the compose file — both servers use it."""
    text = COMPOSE_FILE.read_text()
    m = re.search(r"^  velaris-db:\n(?:    .*\n|\s*\n)*?    image:\s*(\S+)", text, re.M)
    if not m:
        raise Refused("could not read velaris-db's image from the compose file")
    return m.group(1)


# ── main server: the databases ──────────────────────────────────────────────

def compose(*args: str) -> list[str]:
    # The gateway's ports reach compose only once checked (docker-compose.yml).
    os.environ["VELARIS_REPLICATION_GW_PORT"] = setting(*DBS["velaris"][3])
    os.environ["VELARIS_REPLICATION_GW_TEMPORAL_PORT"] = setting(*DBS["temporal"][3])
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if (ROOT / ".env").is_file():
        cmd += ["--env-file", str(ROOT / ".env")]
    return cmd + list(args)


def container_of(service: str) -> str | None:
    r = run(compose("ps", "-q", service), check=False)
    return r.stdout.strip() or None


def psql(key: str, sql: str) -> str:
    """Run SQL in a main database as its superuser. SQL goes on stdin, never
    on a command line (the replicator's password is in it)."""
    service, superuser, db = DBS[key][:3]
    cid = container_of(service)
    if not cid:
        raise Refused(f"{service} is not running on this server")
    r = run(["docker", "exec", "-i", cid, "psql", "-U", superuser, "-d", db,
             "-v", "ON_ERROR_STOP=1", "-tAX", "-f", "-"], input_text=sql)
    return r.stdout.strip()


def main_preflight() -> None:
    kind = (ENV.get("VELARIS_CASE_DATABASE_BACKEND") or yaml_get("database") or "postgresql").lower()
    if kind != "postgresql":
        raise Refused(f"the database is {kind}: a standby is PostgreSQL-only in this release "
                      "(MySQL / MariaDB: December)")
    ext = setting("VELARIS_EXTERNAL_SERVICES", "").lower()
    for part in ("database", "temporal"):
        if re.search(rf"(^|[,\s]){part}([,\s]|$)", ext):
            raise Refused(f"the {part} runs on another server (VELARIS_EXTERNAL_SERVICES) — "
                          "its own replication is what protects it, not this command")
    import shutil
    if not shutil.which("rsync"):
        raise Refused("rsync is not installed on this server (the standby copies documents with it)")
    for key in DBS:
        psql(key, "SELECT 1")


def scram_verifier(password: str, iterations: int = 4096) -> str:
    """PostgreSQL's SCRAM-SHA-256 verifier, computed here: the database is given
    the hash, never the password (nothing to leak into a log or a statement
    view). Same construction as libpq's PQencryptPasswordConn."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored = hashlib.sha256(client_key).digest()
    server = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored)}:{b64(server)}"


def main_prepare(password: str) -> None:
    """Role, pg_hba line, a fresh slot and the WAL cap — in each database.
    All of it is reloadable: neither database restarts."""
    cap = setting("VELARIS_STANDBY_MAX_WAL_KEEP", "10GB")
    if not re.fullmatch(r"\d+\s*(kB|MB|GB|TB)", cap):
        raise Refused(f"VELARIS_STANDBY_MAX_WAL_KEEP={cap!r} — expected e.g. 10GB")
    for key in DBS:
        busy = psql(key, f"SELECT count(*) FROM pg_replication_slots "
                         f"WHERE slot_name = '{SLOT}' AND active")
        if busy != "0":
            raise Refused(f"{key}: a standby is streaming from this server right now — "
                          "run `standby-velaris.sh unlink` first")
        psql(key, f"""
DO $$BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
    CREATE ROLE {ROLE} WITH REPLICATION LOGIN;
  END IF;
END$$;
ALTER ROLE {ROLE} WITH REPLICATION LOGIN NOSUPERUSER PASSWORD '{scram_verifier(password)}';
SELECT pg_drop_replication_slot('{SLOT}') FROM pg_replication_slots WHERE slot_name = '{SLOT}';
SELECT 1 FROM pg_create_physical_replication_slot('{SLOT}');
ALTER SYSTEM SET max_slot_wal_keep_size = '{cap.replace(' ', '')}';
""")
        hba_add(key)
        psql(key, "SELECT pg_reload_conf()")
        bad = psql(key, "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")
        if bad != "0":
            raise RuntimeError(f"{key}: pg_hba.conf has an error after the change")


def _hba_exec(key: str, script: str) -> None:
    cid = container_of(DBS[key][0])
    run(["docker", "exec", "-e", f"MARK={HBA_MARK}", "-e", f"LINE={HBA_LINE}", cid,
         "sh", "-c", script])


def hba_add(key: str) -> None:
    _hba_exec(key, 'f="$PGDATA/pg_hba.conf"; grep -qxF "$LINE" "$f" || '
                   'printf "%s\\n%s\\n" "$MARK" "$LINE" >> "$f"')


def hba_remove(key: str) -> None:
    # Rewritten in place (same inode, same owner) without the two lines.
    _hba_exec(key, 'f="$PGDATA/pg_hba.conf"; t="$(grep -vxF -e "$MARK" -e "$LINE" "$f")"; '
                   'printf "%s\\n" "$t" > "$f"')


def main_teardown() -> list[str]:
    """Undo main_prepare. Each part independent: one failing does not stop the rest."""
    notes = []
    for key in DBS:
        try:
            psql(key, f"""
SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots
 WHERE slot_name = '{SLOT}' AND active_pid IS NOT NULL;
SELECT pg_sleep(0.5);
SELECT pg_drop_replication_slot('{SLOT}') FROM pg_replication_slots WHERE slot_name = '{SLOT}';
DROP ROLE IF EXISTS {ROLE};
ALTER SYSTEM RESET max_slot_wal_keep_size;
""")
            hba_remove(key)
            psql(key, "SELECT pg_reload_conf()")
        except Exception as e:  # noqa: BLE001
            notes.append(f"{key}: {e}")
    return notes


def main_db_state() -> dict:
    """What each main database knows about its standby — no SSH needed."""
    out = {}
    for key in DBS:
        row = psql(key, f"""
SELECT coalesce(s.active::text, ''), coalesce(s.wal_status, ''),
       coalesce(r.state, ''),
       coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), r.replay_lsn)::bigint::text, ''),
       coalesce(round(extract(epoch FROM r.replay_lag)::numeric, 3)::text, ''),
       coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), s.restart_lsn)::bigint::text, ''),
       coalesce(extract(epoch FROM now() - r.reply_time)::int::text, '')
  FROM (SELECT 1) one
  LEFT JOIN pg_replication_slots s ON s.slot_name = '{SLOT}'
  LEFT JOIN pg_stat_replication r ON r.application_name = '{APP_NAME}'
 LIMIT 1""")
        active, wal_status, state, lag_bytes, lag_s, retained, since = (row.split("|") + [""] * 7)[:7]
        if not wal_status:
            st = "no_slot"
        elif wal_status == "lost":
            st = "needs_relink"
        elif state == "streaming" and since and int(since) > STALL_SECONDS:
            # PostgreSQL notices a dead standby only after wal_sender_timeout
            # (60 s); until then the row still says streaming. The standby
            # answers every 10 s, so silence past STALL_SECONDS is the truth.
            st = "stalled"
        elif state == "streaming":
            st = "streaming"
        elif state:
            st = state          # startup / catchup / backup
        else:
            st = "disconnected"
        out[key] = {"state": st, "slot_wal_status": wal_status or None,
                    "lag_bytes": int(lag_bytes) if lag_bytes else None,
                    "replay_lag_seconds": float(lag_s) if lag_s else (0.0 if st == "streaming" else None),
                    "wal_retained_bytes": int(retained) if retained else None,
                    "seconds_since_reply": int(since) if since else None}
    return out


def docs_source() -> tuple[Path | None, str]:
    """The main's document folder, or why there is none to copy."""
    backend = (ENV.get("VELARIS_CASE_STORAGE_BACKEND") or "local").lower()
    if backend != "local":
        ep = (ENV.get("VELARIS_CASE_S3_ENDPOINT") or "").lower().split(":")[0]
        bundled = backend == "minio" or ep in ("", "localhost", "127.0.0.1", "minio", "velaris-minio")
        return None, ("documents are in the bundled MinIO — NOT copied; move them to local "
                      "storage (scripts/storage-migrate.py) or an S3 service" if bundled else
                      f"documents are in object storage ({backend}) — held by that service, not copied")
    raw = ENV.get("VELARIS_CASE_STORAGE_LOCAL_PATH") or "./data/velaris-docs"
    return (Path(raw) if os.path.isabs(raw) else (ROOT / raw).resolve()), ""


def folder_source(name: str) -> tuple[Path | None, str]:
    """A folder the standby copies, or why there is none."""
    if name == "docs":
        return docs_source()
    if name == "deletions":
        raw = ENV.get("VELARIS_CASE_DELETION_LEDGER_DIR") or ""
        p = (Path(raw) if os.path.isabs(raw) else (ROOT / raw).resolve()) if raw else ROOT / ".deletions"
        return (p, "") if p.is_dir() else (None, "no deleted accounts purged yet")
    raw = ENV.get("VELARIS_CASE_MEET_RECORDINGS_DIR") or ""
    if not raw:
        return None, "meeting recordings are off (VELARIS_CASE_MEET_RECORDINGS_DIR empty)"
    return (Path(raw) if os.path.isabs(raw) else (ROOT / raw).resolve()), ""


def unsafe_folder(p: Path) -> str | None:
    """A copied folder is mirrored with deletions, on both servers' accounts:
    it must never be one that holds anything else. A setting such as
    VELARIS_CASE_STORAGE_LOCAL_PATH=. would otherwise serve the whole install
    (keys included) and, on the standby, delete it."""
    p = Path(os.path.abspath(p))
    home = Path.home()
    if p in (Path("/"), home, ROOT) or ROOT.is_relative_to(p):
        return f"{p} contains the install or the home folder"
    for inside in (ROOT / ".standby", ROOT / "deploy", ROOT / "scripts", ROOT / ".venv",
                   home / ".ssh", Path("/etc"), Path("/usr"), Path("/var/lib/docker")):
        if p == inside or p.is_relative_to(inside):
            return f"{p} is inside {inside}"
    return None


# ── the code the standby follows (part d) ──────────────────────────────────
# What is NOT code: this server's own identity, secrets, data and build output.
# The mirror's exclude list (CLAUDE.md: "an install generates its own identity")
# plus what an install accumulates at run time.
_CODE_SKIP_NAMES = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
                    ".vite", "dist", "dist.new", "dist.old", ".standby", ".backups", "logs", "fonts"}
_CODE_SKIP_PATHS = {".env", "velaris-install-report.md", "git_push.sh", "data",
                    "services/case-service/data", "migrations/mysql/historical",
                    "deploy/tls/certs", "deploy/helm/velaris/charts", "deploy/openbao/agent/role_id",
                    "deploy/openbao/agent/secret_id", "deploy/openbao/agent/out",
                    "deploy/openbao/.bao-init.json", "deploy/openbao/enabled",
                    "deploy/docker-compose/.env", "deploy/docker-compose/egress-gw/conf/allowlist.json",
                    "deploy/docker-compose/egress-gw/log"}


# What a release holds (the release twin's top level). Anything else at the top
# is this install's own — and an install writes things nobody listed in advance
# (seen 2026-10-22 on the dev box: `.velaris-key`, a `tenant_deks-unreadable-*.json`
# dump, `*_cred.txt`). So: allowed by name, never by "not on a deny-list".
_CODE_TOP_DIRS = {"K8", "codegen", "codegen-components", "deploy", "engine", "libs", "migrations",
                  "models", "packs", "plugins", "releases", "scout", "scripts", "services", "studio"}
_CODE_TOP_FILES = {".env.example", ".gitignore", "LICENSE", "LICENSE-APACHE", "Makefile", "README.md",
                   "package.json", "pyproject.toml", "uv.lock", "velaris.yaml", "channels.json"}


def not_code(rel: str) -> bool:
    """True for anything in the install that must never travel as 'code'."""
    import fnmatch
    parts = rel.split("/")
    if len(parts) == 1:
        if not (rel in _CODE_TOP_FILES or (rel.endswith(".sh") and not rel.startswith("."))):
            return True
    elif parts[0] not in _CODE_TOP_DIRS:
        return True
    if any(p in _CODE_SKIP_NAMES or p.endswith(".egg-info") for p in parts):
        return True
    if any(rel == q or rel.startswith(q + "/") for q in _CODE_SKIP_PATHS):
        return True
    name = parts[-1]
    # By name, at ANY depth: a secret one folder down is still a secret.
    if name in (".env", ".bao-init.json", "role_id", "secret_id", "velaris-install-report.md",
                "authorized_keys", "known_hosts", "witness_key", "witness_key.pub",
                "witness_known_hosts", "tunnel_key", "reads_key") or (name.startswith(".env.") and name != ".env.example"):
        return True
    return any(fnmatch.fnmatch(name, g) for g in ("*.pyc", "*.log", "*.bak*", "*.before-restore-*",
                                                  "*.velaris-tmp", "*.key", "*.pem", "*.p12", "*.pfx",
                                                  "id_rsa*", "id_ed25519*", "id_ecdsa*"))


def export_code(out) -> None:
    """The install's code as a tar.gz stream — regular files only, never a
    secret (not_code), never the document or recording folders."""
    import tarfile
    skip_dirs = {p for p in (folder_source(n)[0] for n in FOLDERS) if p}
    with tarfile.open(fileobj=out, mode="w|gz") as tar:
        for dirpath, dirs, files in os.walk(ROOT):
            here = Path(dirpath)
            rel_dir = os.path.relpath(here, ROOT)
            dirs[:] = [d for d in dirs if not (here / d).is_symlink()
                       and (rel_dir != "." or d in _CODE_TOP_DIRS)
                       and not not_code(os.path.normpath(os.path.join(rel_dir, d, "_")))
                       and (here / d).resolve() not in skip_dirs]
            for f in files:
                rel = os.path.normpath(os.path.join(rel_dir, f))
                full = here / f
                if not_code(rel) or full.is_symlink() or not full.is_file():
                    continue
                tar.add(full, arcname=rel, recursive=False)


def _log_refusal(req: str) -> None:
    subprocess.run(["logger", "-t", "velaris-standby-serve", f"refused request: {req[:120]!r}"],
                   capture_output=True)


def _has_a_file(root: Path) -> bool:
    """Stops at the first file: cheap on any size of store."""
    for _dirpath, _dirs, files in os.walk(root):
        if files:
            return True
    return False


def _bao_token() -> str | None:
    try:
        return json.loads((OPENBAO_DIR / ".bao-init.json").read_text())["root_token"]
    except Exception:  # noqa: BLE001
        return None


def cmd_serve(_a: argparse.Namespace | None = None) -> int:
    """The main's side of the standby's key (authorized_keys `command=`).
    Answers exactly these requests; anything else is refused and logged.
    Everything it hands out the standby must hold to be promotable — the
    standby is protected like the main for that reason (runbook §8)."""
    req = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    out = sys.stdout.buffer
    if not MAIN_RECORD.exists() or DEMOTED.exists():
        print("not linked" if not MAIN_RECORD.exists() else "demoted", file=sys.stderr)
        return 1
    if req == "demote":
        print(json.dumps(demote_here()))
        return 0
    if req == "failover heartbeat":
        # Step 12: the standby's heartbeat — recorded, and answered with this
        # server's term, fence state and WAL position.
        print(json.dumps(_failover().heartbeat()))
        return 0
    m = RSYNC_SENDER_RE.fullmatch(req)
    if m:
        src, why = folder_source(m.group(1))
        bad = unsafe_folder(src) if src else None
        if not src or bad or not src.is_dir():
            print(why or bad or f"folder {src} missing", file=sys.stderr)
            return 1
        args = req.split()
        args[-1] = f"{src}/"
        os.execvp("rsync", args)     # the sender, rooted at that folder only
    if req == "export env":
        out.write((ROOT / ".env").read_bytes())
        return 0
    if req == "export code":
        export_code(out)
        return 0
    if req == "export identity":
        print(json.dumps({n: (ROOT / n).read_text() for n in IDENTITY_FILES if (ROOT / n).is_file()}))
        return 0
    if req == "export config":
        out.write((ROOT / "velaris.yaml").read_bytes())
        return 0
    if req.startswith("export folder-info ") and req.split()[-1] in FOLDERS:
        src, why = folder_source(req.split()[-1])
        if src and unsafe_folder(src):
            src, why = None, f"NOT copied: {unsafe_folder(src)}"
        print(json.dumps({"path": str(src) if src else None, "why": why,
                          "has_files": bool(src) and _has_a_file(src),
                          "rel": os.path.relpath(src, ROOT) if src and src.is_relative_to(ROOT) else None}))
        return 0
    if req == "export vault-index":
        if not (OPENBAO_DIR / "enabled").is_file():
            print("off")
            return 0
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:8350/v1/sys/leader", timeout=5) as r:
                print(json.loads(r.read()).get("raft_committed_index", "unknown"))
        except Exception:  # noqa: BLE001
            print("unavailable")
        return 0
    if req == "export vault":
        token = _bao_token()
        if not token:
            print("no root token", file=sys.stderr)
            return 1
        # The token on stdin, never on a command line.
        r = subprocess.run(["docker", "exec", "-i", "-e", "BAO_ADDR=http://127.0.0.1:8200",
                            "velaris-openbao", "sh", "-c",
                            "read -r BAO_TOKEN; export BAO_TOKEN; f=/tmp/velaris-standby.snap; "
                            "bao operator raft snapshot save $f >/dev/null && cat $f; "
                            "rc=$?; rm -f $f; exit $rc"],
                           input=(token + "\n").encode(), stdout=out, stderr=subprocess.PIPE)
        return r.returncode
    if req == "export bao-files":
        print(json.dumps({n: (OPENBAO_DIR / n).read_text() for n in BAO_FILES
                          if (OPENBAO_DIR / n).is_file()}))
        return 0
    if req == "report":
        try:
            rep = json.loads(sys.stdin.read(65536))
        except ValueError:
            return 1
        write_private(SYNC_REPORT, json.dumps({**rep, "received_at": now()}, indent=2))
        try:
            # The standby reports every minute: the status the platform reads
            # (posture, Studio, alerts) is refreshed with it, no timer needed here.
            write_status(main_db_state())
        except Exception:  # noqa: BLE001
            pass
        return 0
    _log_refusal(req)
    print(f"refused: {req[:80]!r}", file=sys.stderr)
    return 1


def demote_here() -> dict:
    """The standby is taking over: make sure this server can no longer take
    writes. The mark goes first, so nothing can start the platform again in
    between (start/update/setup/restore and every unit refuse while it exists).
    Then users lose the door (gateway), then the databases stop — a fast
    shutdown, during which PostgreSQL sends its last WAL to the connected
    standby. The standby's key is revoked: it did its last job."""
    lsn = {}
    for key in DBS:
        try:
            lsn[key] = psql(key, "SELECT pg_current_wal_lsn()")
        except Exception:  # noqa: BLE001
            lsn[key] = None
    write_private(DEMOTED, json.dumps({"demoted_at": now(), "by": os.environ.get(
        "SSH_CONNECTION", "").split(" ")[0], "lsn_before_stop": lsn}, indent=2))
    run(["docker", "stop", "-t", "10", "velaris-gateway"], check=False)
    for svc in ("velaris-db", "temporal-db"):
        cid = container_of(svc)
        if cid:
            run(["docker", "stop", "-t", "60", cid], check=False)
    run(compose("--profile", "standby", "stop", "replication-gw"), check=False)
    for key in DBS:   # what each database wrote last (read from its stopped files)
        cid = run(compose("ps", "-aq", DBS[key][0]), check=False).stdout.strip()
        if cid:
            r = run(["docker", "run", "--rm", "--volumes-from", cid, "--entrypoint", "sh", db_image(),
                     "-c", "pg_controldata /var/lib/postgresql/data | sed -n "
                           "'s/^Latest checkpoint location: *//p'"], check=False)
            if r.stdout.strip():
                lsn[key] = r.stdout.strip()
    revoke_tunnel_key()
    # This server's reads tunnel led to the standby's replica — the main now.
    reads_tunnel_stop()
    if MAIN_RECORD.exists():
        rec = json.loads(MAIN_RECORD.read_text())
        rec.update(state="demoted", demoted_at=now())
        write_private(MAIN_RECORD, json.dumps(rec, indent=2))
    return {"demoted": True, "final_lsn": lsn}


def cmd_retire(_a: argparse.Namespace) -> int:
    """On a demoted old main: clear it so the new main can link it as its standby."""
    if not DEMOTED.exists():
        raise Refused("this server has not been demoted — `retire` is only for an old main")
    run(compose("--profile", "*", "down"), check=False)          # containers only; volumes stay
    revoke_tunnel_key()
    reads_tunnel_stop()
    for n in reads_url_clear():
        say(f"! {n}")
    for f in (MAIN_RECORD, STATUS_FILE, SYNC_REPORT, STATE / "fenced.json"):
        f.unlink(missing_ok=True)     # fenced.json: step 12's mark — replaced, then retired
    # Services AND their triggers: the update timer/path and the backup timer
    # would otherwise keep firing against a server that must not run (seen
    # 2026-10-22: velaris-update.path stayed armed).
    units = run(["sh", "-c", "systemctl list-unit-files 'velaris-*' --state=enabled "
                             "--no-legend | awk '{print $1}' | grep -v -e standby -e failover"],
                check=False).stdout.split()
    # (Not the standby or failover units: as the new main's standby this server
    # needs its tunnel, its sync and — with automatic failover — its agent.)
    say("✓ Retired: containers removed (volumes kept), records cleared. The demoted mark stays:")
    say("  nothing will start the platform here.")
    if units:
        say(f"  Its platform units are still enabled — stop them: sudo systemctl disable --now {' '.join(units)}")
    say("  To make it the new main's standby: `sudo ./deploy/systemd/install-standby-tunnel.sh` here,")
    say("  then on the new main: `./standby-velaris.sh link <this server> --replace-standby-data`.")
    return 0


def gw(*args: str) -> None:
    run(compose("--profile", "standby", *args))


# ── main server: SSH to the standby ─────────────────────────────────────────

class Remote:
    """The operator's own SSH session to the standby, multiplexed so a
    password or key prompt is answered once."""

    def __init__(self, target: str, port: int, standby_dir: str) -> None:
        self.target, self.port, self.dir = target, port, standby_dir
        private_dir()
        self.base = ["ssh", "-p", str(port), "-o", "ConnectTimeout=15",
                     "-o", "ControlMaster=auto", "-o", f"ControlPath={STATE}/cm-%C",
                     "-o", "ControlPersist=120", target]

    def call(self, cmd: str, payload: dict | None = None, check: bool = True) -> dict:
        remote = f"cd {shlex.quote(self.dir)} && ./standby-velaris.sh {cmd}"
        r = subprocess.run(self.base + [remote], input=json.dumps(payload or {}),
                           capture_output=True, text=True)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            msg = (r.stderr or r.stdout).strip()[-600:]
            raise Refused(f"standby: `{cmd}` gave no answer (exit {r.returncode}): {msg}")
        res = json.loads(lines[-1])
        if check and not res.get("ok"):
            raise Refused(f"standby: {res.get('error') or res}")
        return res

    def close(self) -> None:
        subprocess.run(["ssh", "-o", f"ControlPath={STATE}/cm-%C", "-O", "exit", self.target],
                       capture_output=True)


def host_key_lines(host: str, port: int) -> str:
    name = host if port == 22 else f"[{host}]:{port}"
    lines = []
    for p in sorted(Path("/etc/ssh").glob("ssh_host_*_key.pub")):
        parts = p.read_text().split()
        if len(parts) >= 2:
            lines.append(f"{name} {parts[0]} {parts[1]}")
    if not lines:
        raise Refused("no SSH host keys in /etc/ssh on this server — is sshd installed?")
    return "\n".join(lines) + "\n"


def authorized_keys_path() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


def authorize_tunnel_key(pubkey: str, standby: str) -> None:
    """Let the standby's key forward to the two gateway ports — and nothing else."""
    parts = pubkey.split()
    if (len(parts) < 2 or not re.fullmatch(r"ssh-[a-z0-9-]+", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1])):
        raise Refused(f"the standby sent no usable key: {pubkey[:60]!r}")
    p1, p2 = setting(*DBS["velaris"][3]), setting(*DBS["temporal"][3])
    # `port-forwarding` re-enables REMOTE forwards (-R) too, and authorized_keys
    # has no "none" for them; with GatewayPorts=no sshd binds any -R on this
    # server's loopback whatever address is asked for (proven 2026-10-22) — a
    # listener a stolen key could put where a restarting service expects its
    # port. So -R may only ask for the gateway's own port, which the gateway
    # already holds: every remote forward fails.
    opts = (f'restrict,port-forwarding,permitopen="127.0.0.1:{p1}",'
            f'permitopen="127.0.0.1:{p2}",permitlisten="127.0.0.1:{p1}",'
            f'command="{serve_command()}"')
    line = f"{opts} {parts[0]} {parts[1]} {KEY_MARK} {re.sub(r'[^A-Za-z0-9@._:-]', '', standby)}"
    path = authorized_keys_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    keep = [ln for ln in (path.read_text().splitlines() if path.exists() else [])
            if f" {KEY_MARK} " not in ln]
    write_text_0600(path, "\n".join(keep + [line]) + "\n")


def serve_command() -> str:
    """The only thing the standby's key may run here (`-N` tunnels run nothing)."""
    cmd = f"{ROOT}/standby-velaris.sh serve"
    if not re.fullmatch(r"[A-Za-z0-9/._ -]+", cmd):
        raise Refused(f"the install path {ROOT} cannot be written into authorized_keys safely")
    return cmd


def revoke_tunnel_key() -> bool:
    path = authorized_keys_path()
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    keep = [ln for ln in lines if f" {KEY_MARK} " not in ln]
    if len(keep) != len(lines):
        write_text_0600(path, "\n".join(keep) + ("\n" if keep else ""))
        return True
    return False


def write_text_0600(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".velaris-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def wait_streaming(timeout: int = 120) -> dict:
    end = time.monotonic() + timeout
    while True:
        st = main_db_state()
        if all(v["state"] == "streaming" for v in st.values()) or time.monotonic() > end:
            return st
        time.sleep(2)


def cmd_link(a: argparse.Namespace) -> int:
    if MAIN_RECORD.exists():
        rec = json.loads(MAIN_RECORD.read_text())
        raise Refused(f"already linked to {rec.get('standby')} ({rec.get('state')}) — "
                      "`standby-velaris.sh unlink` first")
    if (STATE / "standby.json").exists():
        raise Refused("this server is itself a standby — link from the main server")
    target = a.host if "@" in a.host else f"{getpass.getuser()}@{a.host}"
    user, host = target.split("@", 1)
    # Both end up on an ssh command line and in the standby's tunnel unit.
    if not (USER_RE.fullmatch(user) and HOST_RE.fullmatch(host)):
        raise Refused(f"not a user@host: {a.host!r}")
    if a.main_address and not HOST_RE.fullmatch(a.main_address):
        raise Refused(f"not an address: {a.main_address!r}")
    main_preflight()
    remote = Remote(target, a.ssh_port, a.standby_dir or str(ROOT))
    image = db_image()
    try:
        say(f"→ Checking {target} …")
        chk = remote.call("remote-check", {"version": version(), "image": image,
                                           "replace": a.replace_standby_data})
        if chk["version"] != version():
            raise Refused(f"the standby has Velaris {chk['version']}, this server {version()} — "
                          "install the same release there first")
        if not chk.get("image"):
            say(f"→ Copying the database image {image} to the standby …")
            save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)
            load = subprocess.run(remote.base + ["docker", "load"], stdin=save.stdout,
                                  capture_output=True, text=True)
            save.wait()
            if save.returncode or load.returncode:
                raise Refused(f"could not copy {image}: {load.stderr.strip()[-300:]}")
        main_address = a.main_address or chk["main_address"]
        if not main_address or not HOST_RE.fullmatch(main_address):
            raise Refused("could not tell this server's address as the standby sees it — "
                          "pass --main-address")

        if FENCE_ENV.exists() and _same_host(read_env(FENCE_ENV).get("MAIN_HOST", ""), host):
            # The old main is being linked as this server's standby: it has been
            # retired by hand (its key is revoked), so there is nothing to fence.
            for f in (FENCE_ENV, STATE / "tunnel_key", STATE / "tunnel_key.pub", STATE / "known_hosts"):
                f.unlink(missing_ok=True)
            say("  (the old main is being linked as the standby — its fence is no longer needed)")
        say("→ Preparing both databases here (role, slot, WAL cap — reload, no restart) …")
        record = {"standby": host, "ssh_target": target, "ssh_port": a.ssh_port,
                  "standby_dir": remote.dir, "main_address": main_address,
                  "linked_at": now(), "state": "linking", "version": version()}
        write_private(MAIN_RECORD, json.dumps(record, indent=2))
        password = secrets.token_urlsafe(32)
        main_prepare(password)
        gw("up", "-d", "replication-gw")

        say("→ Trusting the standby's tunnel key (forwarding to the gateway only) …")
        key = remote.call("remote-key")["pubkey"]
        authorize_tunnel_key(key, host)

        say("→ Copying both databases and starting replication (time grows with data) …")
        res = remote.call("remote-start", {
            "main_host": main_address, "main_ssh_port": a.main_ssh_port,
            "main_user": getpass.getuser(),
            "known_hosts": host_key_lines(main_address, a.main_ssh_port),
            "password": password, "image": image,
            "main_ports": {k: setting(*DBS[k][3]) for k in DBS},
            "replace": a.replace_standby_data})
        if res.get("saved_old_databases"):
            say(f"  The standby's old databases were saved first: {host}:{res['saved_old_databases']}")
            if PROMOTED.exists():
                pr = json.loads(PROMOTED.read_text())
                pr["old_main_saved"] = {"host": host, "path": res["saved_old_databases"], "at": now()}
                write_private(PROMOTED, json.dumps(pr, indent=2))
        st = wait_streaming()
        if not a.skip_models:
            copy_models(remote)
        say("→ First copy of documents, settings and vault (then every 60 s) …")
        first = remote.call("remote-sync", check=False)
        for k in ("env", "config", *FOLDERS, "vault"):
            v = first.get(k) or {}
            say(f"  {k:9} {v.get('state')}{' — ' + v['note'] if v.get('note') else ''}")
        record.update(state="linked" if all(v["state"] == "streaming" for v in st.values())
                      else "linked_not_streaming", copy_seconds=res.get("seconds"))
        write_private(MAIN_RECORD, json.dumps(record, indent=2))
        write_status(st)
        for k, v in st.items():
            say(f"  {k:9} {v['state']}")
        if record["state"] != "linked":
            say("✗ Linked, but not every database is streaming yet — `standby-velaris.sh status`")
            return 3
        hxdr_record("link", host, {"copy_seconds": record.get("copy_seconds"),
                                   "databases": {k: v["state"] for k, v in st.items()}})
        say(f"✓ {host} is this server's standby (asynchronous — loss on switch-over ≈ the lag).")
        return 0
    except BaseException:
        if MAIN_RECORD.exists():
            say("✗ Link failed — undoing it on both servers …")
            try:
                remote.call("remote-stop", check=False)
            except Exception:  # noqa: BLE001
                say("! could not reach the standby to undo it there")
            for n in main_teardown():
                say(f"! {n}")
            revoke_tunnel_key()
            try:
                gw("rm", "-sf", "replication-gw")
            except Exception:  # noqa: BLE001
                pass
            MAIN_RECORD.unlink(missing_ok=True)
            STATUS_FILE.unlink(missing_ok=True)
        raise
    finally:
        remote.close()


def copy_models(remote: Remote) -> None:
    """Ollama's models, once: large, and an air-gapped standby cannot pull them."""
    vol = f"{project()}_ollama-data"
    if not docker_ok("volume", "inspect", vol):
        return
    image = db_image()
    has = remote.call("remote-has-volume", {"volume": vol, "image": image}).get("has_data")
    if has:
        say("  models    already on the standby")
        return
    say("→ Copying Ollama's models to the standby (once; can be several GB) …")
    tar = subprocess.Popen(["docker", "run", "--rm", "-v", f"{vol}:/v:ro", "--entrypoint", "tar",
                            image, "-C", "/v", "-cf", "-", "."], stdout=subprocess.PIPE)
    load = subprocess.run(remote.base + [
        f"docker volume create {vol} >/dev/null && docker run --rm -i -v {vol}:/v "
        f"--entrypoint tar {image} -C /v -xf -"], stdin=tar.stdout, capture_output=True, text=True)
    tar.wait()
    if tar.returncode or load.returncode:
        say(f"! models not copied: {load.stderr.strip()[-200:]} — link continues")


def remote_has_volume(p: dict) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", p.get("volume", "")):
        raise Refused("bad volume name")
    return {"has_data": docker_ok("volume", "inspect", p["volume"]) and docker_ok(
        "run", "--rm", "-v", f"{p['volume']}:/v", "--entrypoint", "sh", p["image"], "-c",
        'test -n "$(ls -A /v)"')}


def hxdr_record(event: str, peer: str | None, detail: dict) -> None:
    """HxDR bookkeeping (dr_standby_events), through the platform's own service.
    Never changes the outcome of the command that called it."""
    py = ROOT / ".venv" / "bin" / "python"
    if not py.is_file():
        say("! .venv not found — event not recorded in HxDR")
        return
    # Directly on :5432 like migrations and backups — never through the pooler,
    # which a promote has not started yet (seen 2026-10-22: both promotes'
    # records were lost to "connect call failed … 6432"). Empty = direct.
    env = {**os.environ, "PYTHONPATH": str(ROOT / "services" / "case-service"),
           "VELARIS_CASE_DATABASE_POOLER_URL": ""}
    r = subprocess.run([str(py), "-m", "case_service.compliance.standby_cli", "--event", event,
                        "--peer", peer or "", "--detail", json.dumps(detail, default=str)],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    if r.returncode or "not recorded" in (r.stderr or ""):
        say(f"! {event} not recorded in HxDR: {(r.stderr or '').strip()[-160:]}")


def _same_host(a: str, b: str) -> bool:
    try:
        return a == b or socket.gethostbyname(a) == socket.gethostbyname(b)
    except OSError:
        return False


def write_status(db: dict) -> dict:
    rec = json.loads(MAIN_RECORD.read_text()) if MAIN_RECORD.exists() else {}
    states = [v["state"] for v in db.values()]
    order = ["needs_relink", "no_slot", "disconnected", "stalled", "startup", "catchup", "backup", "streaming"]
    overall = min(states, key=lambda s: order.index(s) if s in order else 0) if states else "unlinked"
    sync = json.loads(SYNC_REPORT.read_text()) if SYNC_REPORT.exists() else None
    if sync and sync.get("received_at"):
        age = (dt.datetime.now(dt.timezone.utc)
               - dt.datetime.fromisoformat(sync["received_at"])).total_seconds()
        sync["seconds_since_report"] = int(age)
    st = {"role": "main" if rec else None, "standby": rec.get("standby"), "version": version(),
          "standby_version": (sync or {}).get("standby_version"),
          "linked_at": rec.get("linked_at"), "link_state": rec.get("state"),
          "state": overall if rec else "unlinked", "databases": db, "sync": sync,
          "checked_at": now()}
    if rec.get("serve_reads"):
        # Down = the platform reads from this server's capped pool (counted in
        # velaris_db_reads_total{route="main_replica_down"}).
        st["serve_reads"] = {"since": rec["serve_reads"].get("at"), "tunnel_up": _sock_up(),
                             "credentials_current": _reads_credentials_current()}
    write_private(STATUS_FILE, json.dumps(st, indent=2))
    return st


def cmd_status(a: argparse.Namespace) -> int:
    if DEMOTED.exists():
        d = json.loads(DEMOTED.read_text())
        say(f"This server was REPLACED by its standby at {d.get('demoted_at')} (demoted): the "
            "platform must not run here. `standby-velaris.sh retire` to bring it back as a standby.")
        return 3
    if FENCE_ENV.exists():
        p = json.loads(PROMOTED.read_text()) if PROMOTED.exists() else {}
        say(f"! The old main ({read_env(FENCE_ENV).get('MAIN_HOST')}) is NOT demoted yet — keep it "
            f"off. Last attempt {p.get('fence_last_attempt')}: {p.get('fence_last_error')}")
    if not MAIN_RECORD.exists():
        if (STATE / "standby.json").exists():
            print(json.dumps(remote_status(), indent=2))
            return 0
        say("No standby linked.")
        return 0
    st = write_status(main_db_state())
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        say(f"Standby {st['standby']} — {st['state']}")
        for k, v in st["databases"].items():
            lag = "" if v["replay_lag_seconds"] is None else f"  lag {v['replay_lag_seconds']} s"
            byt = "" if v["lag_bytes"] is None else f" / {v['lag_bytes']} bytes"
            say(f"  {k:9} {v['state']}{lag}{byt}")
        if st.get("serve_reads"):
            say(f"  reads     from the standby — tunnel {'up' if st['serve_reads']['tunnel_up'] else 'DOWN (reads on this server)'}")
        sy = st["sync"]
        if not sy:
            say("  files     no report from the standby yet")
        else:
            say(f"  files     last report {sy.get('seconds_since_report')} s ago")
            for k, label in (("docs", "documents"), ("recordings", "recordings"),
                             ("deletions", "deletion log"), ("env", "settings"),
                             ("config", "velaris.yaml"), ("identity", "identity"), ("vault", "vault"),
                             ("code", "code")):
                v = sy.get(k) or {}
                say(f"    {label:12} {v.get('state')}{' — ' + v['note'] if v.get('note') else ''}")
    return 0 if st["state"] == "streaming" else 3


def cmd_unlink(a: argparse.Namespace) -> int:
    rec = json.loads(MAIN_RECORD.read_text()) if MAIN_RECORD.exists() else {}
    if DEMOTED.exists():
        # The "standby" in the record is the main now: never reach into it, and
        # this server's databases are stopped for good — only records are left.
        revoked = revoke_tunnel_key()
        reads_tunnel_stop()
        for f in (MAIN_RECORD, STATUS_FILE, SYNC_REPORT):
            f.unlink(missing_ok=True)
        say("✓ Demoted server: link records cleared" + (", key revoked" if revoked else "")
            + ". The demoted mark stays (`retire` to rejoin as a standby).")
        return 0
    if rec.get("serve_reads") or READS_ENV.exists() or _read_url_is_ours():
        # The reads go back to the main before the replica they use goes away.
        for n in reads_off(None if a.local_only else rec):
            say(f"! {n}")
        rec = json.loads(MAIN_RECORD.read_text()) if MAIN_RECORD.exists() else rec
    fo = _failover()
    if fo.read_conf():
        # Step 12: no standby, no failover. The witness stops trusting both
        # servers' keys before the link they protect goes away.
        say("→ Disabling automatic failover first …")
        if fo.cmd_disable(argparse.Namespace(local_only=a.local_only)) != 0:
            say("! failover was only partly disabled (see above) — unlinking anyway")
    if rec and not a.local_only:
        remote = Remote(rec["ssh_target"], rec.get("ssh_port", 22), rec["standby_dir"])
        try:
            remote.call("remote-stop")
            say(f"✓ {rec['standby']}: tunnel and replicas stopped (its copy of the data is kept)")
        except Refused as e:
            say(f"! could not reach the standby ({e}) — stopping this side only. On it, run "
                "`./standby-velaris.sh remote-stop`")
        finally:
            remote.close()
    notes = main_teardown() if container_of("velaris-db") else ["databases not running"]
    revoked = revoke_tunnel_key()
    try:
        gw("rm", "-sf", "replication-gw")
    except Exception as e:  # noqa: BLE001
        notes.append(f"gateway: {e}")
    if notes:
        # The record stays: a slot left behind keeps WAL (up to the cap) and
        # would be invisible once the record is gone. Run unlink again.
        for n in notes:
            say(f"! {n}")
        say("✗ Not fully unlinked" + (" (tunnel key revoked)" if revoked else "")
            + " — fix the above and run `standby-velaris.sh unlink --local-only` again.")
        return 1
    MAIN_RECORD.unlink(missing_ok=True)
    STATUS_FILE.unlink(missing_ok=True)
    SYNC_REPORT.unlink(missing_ok=True)
    hxdr_record("unlink", rec.get("standby"), {"standby_reached": not a.local_only})
    say("✓ Unlinked: slots and replication role dropped, gateway removed"
        + (", tunnel key revoked" if revoked else "") + ".")
    return 0


# ── main server: reads from the standby (R5d 11b part c) ─────────────────

def _url_netloc(url: str) -> tuple[str, str, str]:
    """(scheme, credentials, host:port) of a database URL — passwords may hold
    '@' and ':', so split on the LAST '@' (as velaris-wait-deps.sh does)."""
    scheme, _, rest = url.partition("://")
    netloc = rest.split("/", 1)[0]
    cred, _, hostport = netloc.rpartition("@")
    return scheme, cred, hostport


def read_url_for(sock_dir: Path) -> str:
    """The main's own database URL, pointed at the tunnel's socket: the same
    role and password (the replica is a copy of this database), the same name.
    The direct URL's own host/port and TLS options do not apply to a socket."""
    from urllib.parse import parse_qsl, urlencode
    url = ENV.get("VELARIS_CASE_DATABASE_URL", "")
    scheme, cred, _ = _url_netloc(url)
    if not scheme.startswith("postgresql") or not cred:
        raise Refused("VELARIS_CASE_DATABASE_URL is not a PostgreSQL URL with credentials — "
                      "reads from the standby need this install's own database")
    tail = url.split("://", 1)[1].partition("/")[2]
    db, _, query = tail.partition("?")
    keep = [(k, v) for k, v in parse_qsl(query) if k not in ("host", "port") and not k.startswith("ssl")]
    return f"{scheme}://{cred}@/{db or 'velaris'}?" + urlencode(keep + [("host", str(sock_dir))])


def _read_url_is_ours(value: str | None = None) -> bool:
    """A read URL `serve-reads on` wrote: its host is an install's reads socket
    folder. Recognised by its value — a .env rendered from OpenBao keeps no
    comments — and by the folder's name, so a promoted standby (same or other
    install path) recognises the main's."""
    from urllib.parse import parse_qs
    v = ENV.get(READ_URL_KEY, "") if value is None else value
    if not v or "?" not in v:
        return False
    host = (parse_qs(v.split("?", 1)[1]).get("host") or [""])[-1]
    return host.endswith("/.standby/reads-sock")


def _sock_up() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(1.0)
            c.connect(str(READS_SOCK))
            return True
    except OSError:
        return False


def _env_value(value: str) -> str:
    """As .env needs it: the start scripts SOURCE the file, so a value with '&',
    ';', '$' … is quoted (an unquoted '&' breaks the line); read_env, pydantic
    and compose all strip the quotes."""
    return f'"{value}"' if re.search(r"""[&;|<>$`\s'"()]""", value) else value


def _env_set(path: Path, key: str, value: str) -> None:
    """Set KEY in an env file, keeping every other line (0600, atomic)."""
    value = _env_value(value)
    lines = path.read_text().splitlines() if path.is_file() else []
    out, done = [], False
    for ln in lines:
        if ln.startswith(f"{key}="):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    write_text_0600(path, "\n".join(out) + "\n")


def _bao_patch(values: dict[str, str]) -> str | None:
    """Store keys in OpenBao's secret/velaris/env when this install keeps its
    settings there — otherwise the next boot's render would put the old value
    back (or refuse over a key it does not hold). Token and values on stdin,
    never on a command line. Returns a problem, or None."""
    if not (OPENBAO_DIR / "enabled").is_file():
        return None
    token = _bao_token()
    if not token:
        return "OpenBao holds this install's settings but its token is unreadable — " \
               f"run ./scripts/secrets-push.sh {READ_URL_KEY}=<the same value>"
    r = subprocess.run(["docker", "exec", "-i", "-e", "BAO_ADDR=http://127.0.0.1:8200", "velaris-openbao",
                        "sh", "-c", "read -r BAO_TOKEN; export BAO_TOKEN; "
                                    "bao kv patch secret/velaris/env @/dev/stdin >/dev/null"],
                       input=(token + "\n" + json.dumps(values)).encode(), capture_output=True)
    if r.returncode:
        return ("could not store the setting in OpenBao (sealed?): "
                + r.stderr.decode(errors="replace").strip()[-160:]
                + " — run ./scripts/secrets-unseal.sh, then ./scripts/secrets-push.sh --sync-env")
    return None


def reads_url_clear(env_file: Path | None = None) -> list[str]:
    """Empty the read URL when it is the one serve-reads wrote (an operator's
    own replica is never touched). Empty = every read on the main, as before."""
    env_file = env_file or ROOT / ".env"
    if not _read_url_is_ours(read_env(env_file).get(READ_URL_KEY, "")):
        return []
    # The vault first: with .env emptied but the vault still holding the URL,
    # the next boot's render would quietly turn it back on.
    problem = _bao_patch({READ_URL_KEY: ""})
    if problem:
        return [problem + " — the read URL is left as it is"]
    _env_set(env_file, READ_URL_KEY, "")
    return []


def reads_tunnel_stop() -> None:
    READS_ENV.unlink(missing_ok=True)
    # systemd restarts it, finds no reads-tunnel.env, and leaves it stopped.
    run(["pkill", "-u", str(os.getuid()), "-f", f"[s]sh -N -T -i {READS_KEY}"], check=False)
    READS_SOCK.unlink(missing_ok=True)


def _replica_answers(url: str) -> str:
    """Through the tunnel, as the platform will connect: a replica of THIS
    database (same system identifier, in recovery)? The password goes in the
    environment of a short-lived client, never on a command line."""
    from urllib.parse import unquote
    _, cred, _ = _url_netloc(url)
    user, _, password = cred.partition(":")
    db = url.split("://", 1)[1].partition("/")[2].split("?", 1)[0] or "velaris"
    r = subprocess.run(["docker", "run", "--rm", "-i", "-v", f"{READS_SOCK_DIR}:/s", "-e", "PGPASSWORD",
                        "--entrypoint", "psql", db_image(), "-h", "/s", "-p", "5432",
                        "-U", unquote(user), "-d", db, "-tAX", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                       # A boolean joined to text reads "true"/"false", not psql's
                       # "t"/"f" (clean box 2026-10-23: every real replica refused).
                       input="SELECT pg_is_in_recovery()::text || '|' || "
                             "(SELECT system_identifier::text FROM pg_control_system());",
                       capture_output=True, text=True, env={**os.environ, "PGPASSWORD": unquote(password)},
                       timeout=60)
    if r.returncode:
        return f"the replica did not answer through the tunnel: {r.stderr.strip()[-200:]}"
    in_recovery, _, sysid = r.stdout.strip().partition("|")
    main_id = psql("velaris", "SELECT system_identifier::text FROM pg_control_system()")
    if sysid != main_id:
        return "the database behind the tunnel is not a copy of this one (system identifier)"
    if in_recovery not in ("true", "t"):
        return "the database behind the tunnel is not in recovery — not a replica"
    return ""


def reads_off(rec: dict | None, *, clear_url: bool = True) -> list[str]:
    """Reads back on the main: the setting first (the services read it at their
    next start), then the tunnel, then the standby's trust in the key. `rec`
    None = do not contact the standby."""
    notes = reads_url_clear() if clear_url else []
    reads_tunnel_stop()
    if rec:
        remote = Remote(rec["ssh_target"], rec.get("ssh_port", 22), rec["standby_dir"])
        try:
            remote.call("remote-reads-revoke")
        except Refused as e:
            notes.append(f"could not reach the standby to revoke the reads key ({e}) — on it: "
                         "./standby-velaris.sh remote-reads-revoke </dev/null")
        finally:
            remote.close()
    for f in (READS_KEY, READS_KEY.with_suffix(".pub"), READS_KNOWN):
        f.unlink(missing_ok=True)
    if MAIN_RECORD.exists():
        r = json.loads(MAIN_RECORD.read_text())
        r.pop("serve_reads", None)
        write_private(MAIN_RECORD, json.dumps(r, indent=2))
    return notes


def _reads_credentials_current() -> bool:
    """The read URL was made from the main's database URL; a rotated password
    would leave it failing (reads quietly on the capped pool)."""
    ours = ENV.get(READ_URL_KEY, "")
    return not _read_url_is_ours(ours) or \
        _url_netloc(ours)[1] == _url_netloc(ENV.get("VELARIS_CASE_DATABASE_URL", ""))[1]


READ_UNITS = ("velaris-case-service", "velaris-analytics-service", "velaris-hxgraph-service",
              "velaris-user-service", "velaris-commerce-service")


def restart_hint() -> str:
    """The services that read the setting, as units this box has: user- and
    commerce-service run inside case-service's unit until their own is
    installed, and a unit systemd does not know fails the whole command."""
    units = [u for u in READ_UNITS if subprocess.run(
        ["systemctl", "cat", u], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0] if shutil.which("systemctl") else []
    how = f"sudo systemctl restart {' '.join(units)} — or " if units else ""
    return f"The services read it when they start: restart them ({how}./start-velaris.sh)."


def cmd_serve_reads(a: argparse.Namespace) -> int:
    rec = json.loads(MAIN_RECORD.read_text()) if MAIN_RECORD.exists() else {}
    if a.action == "status":
        on = rec.get("serve_reads")
        if not on:
            say("Reads from the standby: off" + (" (an operator's own read URL is set)"
                                                 if ENV.get(READ_URL_KEY) and not _read_url_is_ours() else ""))
            return 0
        up = _sock_up()
        say(f"Reads from the standby: on since {on.get('at')} — tunnel {'up' if up else 'DOWN'}. "
            "Down = those reads run on this server (a small capped pool).")
        if not _reads_credentials_current():
            say("! The read URL's credentials differ from VELARIS_CASE_DATABASE_URL (rotated?) — "
                "`serve-reads off`, then `on` again.")
            return 3
        return 0 if up else 3
    if a.action == "off":
        if not (rec.get("serve_reads") or READS_ENV.exists() or _read_url_is_ours()):
            say("Reads from the standby are already off.")
            return 0
        notes = reads_off(rec or None)
        for n in notes:
            say(f"! {n}")
        hxdr_record("serve_reads_off", rec.get("standby"), {})
        say("✓ Reads from the standby: off — every read on this server's database. " + restart_hint())
        return 1 if notes else 0

    # on
    if os.geteuid() == 0:
        raise Refused("run as the install's owner, not root — the tunnel runs as the owner")
    if DEMOTED.exists():
        raise Refused("this server was replaced by its standby — it serves nothing")
    if not rec or rec.get("state") not in ("linked", "linked_not_streaming"):
        raise Refused("no standby is linked — `standby-velaris.sh link <host>` first")
    if rec.get("serve_reads"):
        say(f"Reads from the standby are already on (since {rec['serve_reads'].get('at')}).")
        return 0
    if ENV.get(READ_URL_KEY) and not _read_url_is_ours():
        raise Refused(f"{READ_URL_KEY} is already set to another replica — this install's operator "
                      "chose that; empty it first to use the standby instead")
    if ENV.get("VELARIS_CASE_DB_ANALYTICS_URL"):
        say("  (VELARIS_CASE_DB_ANALYTICS_URL is set: the analytics paths keep using it — it wins for them)")
    if not READS_UNIT.is_file():
        raise Refused("the reads tunnel unit is not installed on this server — once: "
                      "sudo ./deploy/systemd/install-standby-reads.sh")
    url = read_url_for(READS_SOCK_DIR)
    if not re.fullmatch(r"[A-Za-z0-9/._-]+", str(READS_SOCK_DIR)):
        raise Refused(f"the install path {ROOT} cannot be used in the tunnel unit safely")
    if len(str(READS_SOCK).encode()) > 100:       # AF_UNIX paths stop at 108 bytes
        raise Refused(f"the install path {ROOT} is too long for the tunnel's socket "
                      f"({len(str(READS_SOCK))} of at most 100 characters)")
    env_file = ROOT / ".env"
    env_before = env_file.read_text() if env_file.is_file() else None
    vault_written = False
    remote = Remote(rec["ssh_target"], rec.get("ssh_port", 22), rec["standby_dir"])
    try:
        say("→ A key that may reach the standby's replica and nothing else …")
        private_dir()
        for f in (READS_KEY, READS_KEY.with_suffix(".pub")):
            f.unlink(missing_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{READS_KEY_MARK}@{socket.gethostname()}",
             "-f", str(READS_KEY)])
        res = remote.call("remote-reads-allow", {
            "pubkey": READS_KEY.with_suffix(".pub").read_text().strip(),
            "main": socket.gethostname(), "name": rec["standby"], "ssh_port": rec.get("ssh_port", 22)})
        # What the standby sent goes into a known_hosts file and a unit's
        # arguments: only host-key lines for the name we connect to, a port.
        name = rec["standby"] if rec.get("ssh_port", 22) == 22 else f"[{rec['standby']}]:{rec['ssh_port']}"
        keys = [ln for ln in str(res.get("host_keys", "")).splitlines() if ln.strip()]
        if not keys or not all(re.fullmatch(rf"{re.escape(name)} (ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9]+) "
                                            r"[A-Za-z0-9+/]+={0,2}", ln) for ln in keys):
            raise Refused("the standby sent host keys that are not plain keys for its own name")
        if not re.fullmatch(r"[0-9]{1,5}", str(res.get("db_port", ""))):
            raise Refused("the standby sent no usable replica port")
        write_text_0600(READS_KNOWN, "\n".join(keys) + "\n")
        READS_SOCK_DIR.mkdir(mode=0o700, exist_ok=True)
        os.chmod(READS_SOCK_DIR, 0o700)
        user = rec["ssh_target"].split("@", 1)[0]
        write_private(READS_ENV, "".join(f"{k}={v}\n" for k, v in {
            "STANDBY_HOST": rec["standby"], "STANDBY_USER": user,
            "STANDBY_SSH_PORT": rec.get("ssh_port", 22), "STANDBY_DB_PORT": res["db_port"]}.items()))
        say("→ Starting the tunnel (velaris-standby-reads) …")
        end = time.monotonic() + 30
        while not _sock_up():
            if time.monotonic() > end:
                raise Refused("the tunnel did not come up in 30 s — journalctl -u velaris-standby-reads")
            time.sleep(1)
        problem = _replica_answers(url)
        if problem:
            raise Refused(problem)
        say(f"→ Setting {READ_URL_KEY} (this server's database role, through the tunnel) …")
        # The vault first (as for `off`): a .env value the vault lacks would
        # make the next boot's render refuse.
        # Stored as .env holds it (secrets-render writes vault values verbatim).
        problem = _bao_patch({READ_URL_KEY: _env_value(url)})
        if problem:
            raise Refused(problem)
        vault_written = (OPENBAO_DIR / "enabled").is_file()
        _env_set(env_file, READ_URL_KEY, url)
        rec["serve_reads"] = {"at": now()}
        write_private(MAIN_RECORD, json.dumps(rec, indent=2))
    except BaseException:
        say("✗ Not turned on — undoing …")
        remote.close()
        # .env exactly as it was (a key that was not there is not left behind
        # empty — the next OpenBao render would refuse over it); the vault too.
        if env_before is not None and env_file.read_text() != env_before:
            write_text_0600(env_file, env_before)
        if vault_written:
            _bao_patch({READ_URL_KEY: ENV.get(READ_URL_KEY, "")})
        for n in reads_off(rec, clear_url=False):
            say(f"! {n}")
        raise
    finally:
        remote.close()
    hxdr_record("serve_reads_on", rec.get("standby"), {})
    say("✓ Reads from the standby: on. Dashboards, reports, search and HxGraph read from it; "
        "a user's own changes, and everything else, stay on this server.")
    say("  " + restart_hint())
    return 0


# ── standby server (run by the main over SSH; answers one JSON line) ───────

def volume(key: str) -> str:
    return f"{project()}_{DBS[key][0]}"


def docker_ok(*args: str) -> bool:
    return run(["docker", *args], check=False).returncode == 0


def volume_has_data(vol: str, image: str) -> bool:
    if not docker_ok("volume", "inspect", vol):
        return False
    return docker_ok("run", "--rm", "-v", f"{vol}:/d", "--entrypoint", "test", image, "-f", "/d/PG_VERSION")


def platform_running_here() -> str | None:
    """A compose database container in ANY state: a stopped one would start on
    the replicas' volume the next time anything runs `compose up`."""
    for svc in ("velaris-db", "temporal-db"):
        r = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.service={svc}"],
                check=False)
        if r.stdout.strip():
            return f"a {svc} container exists here (docker compose … down to remove it)"
    return None


def remote_check(p: dict) -> dict:
    ssh = os.environ.get("SSH_CONNECTION", "").split()
    image = p.get("image", "")
    out = {"version": version(), "main_address": ssh[0] if ssh else "",
           "image": bool(image) and docker_ok("image", "inspect", image)}
    if not docker_ok("info"):
        raise Refused("Docker is not reachable by this user on the standby")
    if run(["sh", "-c", "command -v rsync"], check=False).returncode:
        raise Refused("rsync is not installed on the standby")
    r = run(["systemctl", "is-enabled", "velaris-standby-sync.timer"], check=False)
    if r.stdout.strip() != "enabled":
        raise Refused("the standby's sync timer is not installed — on the standby re-run "
                      "`sudo ./deploy/systemd/install-standby-tunnel.sh`")
    busy = platform_running_here()
    if busy:
        raise Refused(f"{busy} — a standby must not run Velaris itself")
    r = run(["systemctl", "is-enabled", "velaris-standby-tunnel.path"], check=False)
    if r.stdout.strip() != "enabled":
        raise Refused("the tunnel units are not installed here — on the standby run "
                      "`sudo ./deploy/systemd/install-standby-tunnel.sh`")
    if out["image"] and not p.get("replace"):
        for key in DBS:
            if volume_has_data(volume(key), image):
                raise Refused(f"volume {volume(key)} already holds a database here — pass "
                              "--replace-standby-data to discard it")
    return out


def remote_key(_p: dict) -> dict:
    private_dir()
    key = STATE / "tunnel_key"
    for f in (key, key.with_suffix(".pub")):
        f.unlink(missing_ok=True)
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{KEY_MARK}@{socket.gethostname()}",
         "-f", str(key)])
    return {"pubkey": key.with_suffix(".pub").read_text().strip()}


def _port_open(port: str, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def stop_replicas() -> None:
    for key in DBS:
        run(["docker", "rm", "-f", DBS[key][4]], check=False)


def stop_tunnel() -> None:
    TUNNEL_ENV.unlink(missing_ok=True)
    # systemd restarts it, finds no tunnel.env, and leaves it stopped.
    run(["pkill", "-u", str(os.getuid()), "-f", f"[s]sh -N -T -i {STATE}/tunnel_key"], check=False)


def remote_start(p: dict) -> dict:
    t0 = time.monotonic()
    image = p["image"]
    local = {k: setting(*DBS[k][3]) for k in DBS}      # tunnel's local end = same numbers
    replica_port = {k: setting(*DBS[k][5]) for k in DBS}
    for k, port in replica_port.items():
        if _port_open(port) and not run(["docker", "ps", "-q", "-f", f"name=^{DBS[k][4]}$"],
                                        check=False).stdout.strip():
            raise Refused(f"port {port} is already in use here (needed by the {k} replica — "
                          f"set {DBS[k][5][0]})")
    stop_tunnel()
    stop_replicas()
    time.sleep(1)
    write_private(STATE / "known_hosts", p["known_hosts"])
    write_private(TUNNEL_ENV, "".join(f"{k}={v}\n" for k, v in {
        "MAIN_HOST": p["main_host"], "MAIN_SSH_PORT": p["main_ssh_port"], "MAIN_USER": p["main_user"],
        "LOCAL_DB_PORT": local["velaris"], "MAIN_DB_PORT": p["main_ports"]["velaris"],
        "LOCAL_TEMPORAL_PORT": local["temporal"], "MAIN_TEMPORAL_PORT": p["main_ports"]["temporal"],
    }.items()))
    end = time.monotonic() + 60
    while not all(_port_open(local[k]) for k in DBS):
        if time.monotonic() > end:
            raise Refused("the tunnel to the main did not come up in 60 s — "
                          "`systemctl status velaris-standby-tunnel` on the standby")
        time.sleep(1)

    saved = save_old_main_databases(image) if DEMOTED.exists() else None
    # SCRAM only (PostgreSQL 16 libpq): the tunnel's local port is free for a
    # few seconds whenever it reconnects; whoever bound it then could ask for
    # the password in clear, and libpq would send it (11b (c) audit).
    env = dict(os.environ, PGPASSWORD=p["password"], PGREQUIREAUTH="scram-sha-256")
    for key in DBS:
        vol = volume(key)
        if docker_ok("volume", "inspect", vol):
            if volume_has_data(vol, image) and not p.get("replace"):
                raise Refused(f"volume {vol} already holds a database")
            run(["docker", "volume", "rm", vol])
        run(["docker", "volume", "create", "--label", f"com.docker.compose.project={project()}",
             "--label", f"com.docker.compose.volume={DBS[key][0]}", vol])
        # PostgreSQL refuses a data directory others can read.
        run(["docker", "run", "--rm", "-v", f"{vol}:/d", "--entrypoint", "sh", image, "-c",
             f"chown {PG_UID}:{PG_UID} /d && chmod 700 /d"])
        # The password reaches pg_basebackup through its environment, never argv.
        run(["docker", "run", "--rm", "--network", "host", "-u", PG_UID, "-e", "PGPASSWORD", "-e", "PGREQUIREAUTH",
             "-v", f"{vol}:/var/lib/postgresql/data", "--entrypoint", "pg_basebackup", image,
             "-h", "127.0.0.1", "-p", local[key], "-U", ROLE, "-D", "/var/lib/postgresql/data",
             "-X", "stream", "-S", SLOT, "-c", "fast"], env=env)
        conninfo = (f"host=127.0.0.1 port={local[key]} user={ROLE} "
                    f"password={p['password']} application_name={APP_NAME} require_auth=scram-sha-256")
        run(["docker", "run", "--rm", "-i", "-u", PG_UID, "-v", f"{vol}:/d", "--entrypoint", "sh",
             image, "-c", 'cat >> /d/postgresql.auto.conf && touch /d/standby.signal && '
                          'chmod 600 /d/postgresql.auto.conf'],
            input_text=(f"\n# velaris standby (R5c)\nprimary_conninfo = '{conninfo}'\n"
                        f"primary_slot_name = '{SLOT}'\n"))
        run(["docker", "run", "--rm", "-i", "-u", PG_UID, "-v", f"{vol}:/d", "--entrypoint", "sh",
             image, "-c", f"cat > /d/{REPLICA_HBA_NAME} && chmod 600 /d/{REPLICA_HBA_NAME}"],
            input_text=REPLICA_HBA)
        run(["docker", "run", "-d", "--name", DBS[key][4], "--network", "host",
             "--restart", "unless-stopped", "--label", "velaris.standby=replica",
             "-v", f"{vol}:/var/lib/postgresql/data", image,
             "postgres", "-c", "listen_addresses=127.0.0.1", "-c", f"port={replica_port[key]}",
             "-c", f"hba_file=/var/lib/postgresql/data/{REPLICA_HBA_NAME}"])
    write_private(STATE / "standby.json", json.dumps({
        "role": "standby", "main": p["main_host"], "linked_at": now(), "image": image}, indent=2))
    # A retired old main joining as a standby: standby.json guards it from here.
    DEMOTED.unlink(missing_ok=True)
    PROMOTED.unlink(missing_ok=True)
    end = time.monotonic() + 90
    while True:
        st = remote_status()
        if all(v.get("receiver") == "streaming" for v in st["databases"].values()):
            break
        if time.monotonic() > end:
            raise Refused(f"replicas started but are not streaming: {st['databases']}")
        time.sleep(2)
    return {"seconds": round(time.monotonic() - t0, 1), "saved_old_databases": saved, **st}


def save_old_main_databases(image: str) -> str | None:
    """A demoted old main being linked as a standby: its databases may hold
    writes it took after the switch-over (it was cut off, not down — proven
    2026-10-22: 1,186). Linking replaces them; so they are dumped first, every
    time, to .backups/before-rejoin-<time>/ (0700). Nobody has to remember."""
    out = ROOT / ".backups" / f"before-rejoin-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    done = []
    for key in DBS:
        vol = volume(key)
        if not volume_has_data(vol, image):
            continue
        out.mkdir(parents=True, exist_ok=True)
        os.chmod(ROOT / ".backups", 0o700)
        os.chmod(out, 0o700)
        name = f"velaris-rejoin-dump-{key}"
        run(["docker", "rm", "-f", name], check=False)
        # Socket only, nothing listening: a throwaway server on the old files.
        run(["docker", "run", "-d", "--name", name, "-v", f"{vol}:/var/lib/postgresql/data", image,
             "postgres", "-c", "listen_addresses="])
        try:
            for _ in range(60):
                if run(["docker", "exec", name, "pg_isready", "-U", DBS[key][1]], check=False).returncode == 0:
                    break
                time.sleep(1)
            fd = os.open(out / f"{key}.sql.gz", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                r = subprocess.run(["docker", "exec", name, "sh", "-c",
                                    f"pg_dumpall -U {DBS[key][1]} | gzip"], stdout=f, stderr=subprocess.PIPE)
            if r.returncode:
                raise Refused(f"could not save the old {key} database before replacing it: "
                              f"{r.stderr.decode(errors='replace')[-200:]} — nothing replaced")
            done.append(key)
        finally:
            run(["docker", "rm", "-f", name], check=False)
    return str(out) if done else None


def remote_status(_p: dict | None = None) -> dict:
    dbs = {}
    for key in DBS:
        name, user, db = DBS[key][4], DBS[key][1], DBS[key][2]
        # The container's own socket (trust stays inside the container).
        r = run(["docker", "exec", name, "psql", "-U", user, "-d", db, "-p", setting(*DBS[key][5]),
                 "-tAX", "-c",
                 "SELECT pg_is_in_recovery(), coalesce((SELECT status FROM pg_stat_wal_receiver), ''), "
                 "coalesce(extract(epoch FROM now() - pg_last_xact_replay_timestamp())::int::text, '')"],
                check=False)
        if r.returncode:
            dbs[key] = {"running": False, "error": (r.stderr or "").strip()[-200:]}
            continue
        rec, recv, age = (r.stdout.strip().split("|") + ["", "", ""])[:3]
        dbs[key] = {"running": True, "in_recovery": rec == "t", "receiver": recv or None,
                    "last_replayed_seconds_ago": int(age) if age else None}
    tunnel = run(["systemctl", "is-active", "velaris-standby-tunnel"], check=False).stdout.strip()
    return {"role": "standby" if (STATE / "standby.json").exists() else None,
            "tunnel": tunnel, "databases": dbs}


def remote_reads_allow(p: dict) -> dict:
    """Trust the main's reads key: it may forward to this server's replica of
    velaris-db (loopback) — no command, no terminal, no other port, and no
    remote forward (pinned to the replica's own port, which it holds)."""
    if not (STATE / "standby.json").exists():
        raise Refused("this server is not a linked standby")
    pub = (p.get("pubkey") or "").split()
    if (len(pub) < 2 or not re.fullmatch(r"ssh-[a-z0-9-]+", pub[0])
            or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", pub[1])):
        raise Refused("no usable key")
    port = setting(*DBS["velaris"][5])
    if not _port_open(port):
        raise Refused(f"this standby's replica is not listening on 127.0.0.1:{port}")
    name, ssh_port = str(p.get("name") or ""), int(p.get("ssh_port") or 22)
    if not HOST_RE.fullmatch(name):
        raise Refused("bad host name")
    opts = (f'restrict,port-forwarding,permitopen="127.0.0.1:{port}",'
            f'permitlisten="127.0.0.1:{port}",command="/bin/false"')
    line = (f"{opts} {pub[0]} {pub[1]} {READS_KEY_MARK} "
            f"{re.sub(r'[^A-Za-z0-9@._:-]', '', str(p.get('main') or ''))}")
    path = authorized_keys_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    keep = [ln for ln in (path.read_text().splitlines() if path.exists() else [])
            if f" {READS_KEY_MARK} " not in ln]
    write_text_0600(path, "\n".join(keep + [line]) + "\n")
    return {"host_keys": host_key_lines(name, ssh_port), "db_port": port}


def revoke_reads_key() -> bool:
    path = authorized_keys_path()
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    keep = [ln for ln in lines if f" {READS_KEY_MARK} " not in ln]
    if len(keep) != len(lines):
        write_text_0600(path, "\n".join(keep) + ("\n" if keep else ""))
        return True
    return False


def remote_reads_revoke(_p: dict) -> dict:
    return {"revoked": revoke_reads_key()}


def remote_stop(_p: dict) -> dict:
    if PROMOTED.exists() and not (STATE / "standby.json").exists():
        raise Refused("this server was promoted — it is a main now, not a standby to stop")
    revoke_reads_key()
    stop_tunnel()
    stop_replicas()
    for f in ("tunnel_key", "tunnel_key.pub", "known_hosts", "standby.json", "main.env", "sync.json",
              MAIN_CONFIG_COPY_NAME, "sync.lock"):
        (STATE / f).unlink(missing_ok=True)
    for n in IDENTITY_FILES:
        (IDENTITY_COPY / n).unlink(missing_ok=True)
    if IDENTITY_COPY.is_dir():
        IDENTITY_COPY.rmdir()
    # The main's secrets go; the data copy (database, encrypted documents) stays.
    if VAULT_COPY.is_dir():
        for f in VAULT_COPY.iterdir():
            f.unlink()
        VAULT_COPY.rmdir()
    # Step 12: without a link there is nothing to fail over to — its witness key goes too.
    _failover().clear_local()
    return {"volumes_kept": [volume(k) for k in DBS]}


# ── standby: pull everything that is not a database (part b) ──────────────

def _ssh_main_opts(t: dict, connect_timeout: int = 15) -> list[str]:
    return ["-i", str(STATE / "tunnel_key"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={STATE / 'known_hosts'}",
            "-o", f"ConnectTimeout={connect_timeout}", "-p", str(t["MAIN_SSH_PORT"])]


def ssh_main(request: str, *, data: bytes | None = None, timeout: int = 600,
             connect_timeout: int = 15) -> subprocess.CompletedProcess:
    """One request to the main's `serve` (its forced command for our key)."""
    t = read_env(TUNNEL_ENV)
    return subprocess.run(["ssh", *_ssh_main_opts(t, connect_timeout), f"{t['MAIN_USER']}@{t['MAIN_HOST']}",
                           request], input=data, capture_output=True, timeout=timeout)


def taking_over_path() -> Path:
    """Step 12: a promote is starting — copies stop (read live: tests move STATE)."""
    return STATE / "failover" / "taking-over"


def _err(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or b"").decode(errors="replace").strip()[-200:] or f"exit {r.returncode}"


def sync_folder(name: str, state: dict, allow_deletes: bool = False) -> dict:
    r = ssh_main(f"export folder-info {name}", timeout=60)
    if r.returncode:
        return {"state": "failed", "note": _err(r)}
    info = json.loads(r.stdout)
    if not info.get("path"):
        return {"state": "not_copied", "note": info.get("why")}
    target = (ROOT / info["rel"]) if info.get("rel") else Path(info["path"])
    if info.get("rel") and not Path(os.path.normpath(target)).is_relative_to(ROOT):
        return {"state": "failed", "note": f"refused: {info['rel']!r} leaves the install folder"}
    bad = unsafe_folder(target)
    if bad:
        return {"state": "failed", "note": f"refused to write there: {bad}"}
    if not info.get("rel") and not target.parent.is_dir():
        # An absolute path is usually a mount (a NAS). Creating it here would
        # fill this server's own disk instead — the operator mounts it first.
        return {"state": "failed", "note": f"{target.parent} does not exist here — mount or "
                                          "create it on the standby (same path as on the main)"}
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    prev = state.get(name, {})
    # A main whose document disk failed to mount shows an EMPTY folder (the
    # mount point); mirroring that wipes the standby's copy — the one that is
    # left. Seen 2026-10-22: a count cap alone (500) let all 301 go. So: an
    # empty main folder is never mirrored onto a standby that holds files, and
    # deletions are capped at a share of what the main last had.
    if name in APPEND_ONLY:
        t = read_env(TUNNEL_ENV)
        t0 = time.monotonic()
        r = subprocess.run(["rsync", "-rtp", "--partial", "--stats", "-e",
                            "ssh " + " ".join(shlex.quote(o) for o in _ssh_main_opts(t)),
                            f"{t['MAIN_USER']}@{t['MAIN_HOST']}:{name}/", f"{target}/"],
                           capture_output=True, text=True, timeout=3600)
        files = re.search(r"Number of files: ([\d,]+)", r.stdout or "")
        res = {"seconds": round(time.monotonic() - t0, 1), "path": str(target),
               "files": int(files.group(1).replace(",", "")) if files else None}
        if r.returncode not in (0, 24):
            return {**res, "state": "failed", "note": (r.stderr or "").strip()[-200:]}
        return {**res, "state": "ok", "synced_at": now()}
    if not info.get("has_files") and _has_a_file(target) and not allow_deletes:
        return {**prev, "state": "deletes_held",
                "note": "the main's document folder is EMPTY and this copy is not — not mirrored "
                        "(unmounted disk?). If it is really empty: `standby-velaris.sh sync "
                        "--allow-deletes` on the standby"}
    cap = int(setting("VELARIS_STANDBY_MAX_DELETE", "500"))
    share = max(10, (prev.get("files") or 0) // 10)
    max_delete = 1_000_000_000 if allow_deletes else min(cap, share)
    t = read_env(TUNNEL_ENV)
    t0 = time.monotonic()
    r = subprocess.run(["rsync", "-rtp", "--partial", "--delete-delay", f"--max-delete={max_delete}",
                        "--stats", "-e", "ssh " + " ".join(shlex.quote(o) for o in _ssh_main_opts(t)),
                        f"{t['MAIN_USER']}@{t['MAIN_HOST']}:{name}/", f"{target}/"],
                       capture_output=True, text=True, timeout=6 * 3600)
    files = re.search(r"Number of files: ([\d,]+)", r.stdout or "")
    res = {"seconds": round(time.monotonic() - t0, 1), "path": str(target),
           "files": int(files.group(1).replace(",", "")) if files else None}
    if r.returncode == 25:
        # The count the cap is measured against stays the last good one, or a
        # held deletion would shrink its own limit run after run.
        return {**res, "files": prev.get("files"), "state": "deletes_held",
                "note": f"more than {max_delete} documents deleted on the main since the last copy "
                        "— those deletions are not mirrored. Check the main's document disk; if "
                        "intended: `standby-velaris.sh sync --allow-deletes` on the standby"}
    if r.returncode not in (0, 24):          # 24: a file vanished mid-copy — normal on a live system
        return {**res, "state": "failed", "note": (r.stderr or "").strip()[-200:]}
    return {**res, "state": "ok", "synced_at": now()}


def sync_file(state: dict, key: str, request: str, dest: Path) -> dict:
    r = ssh_main(request, timeout=60)
    if r.returncode or not r.stdout:
        return {**state.get(key, {}), "state": "failed", "note": _err(r)}
    digest = hashlib.sha256(r.stdout).hexdigest()
    prev = state.get(key, {})
    if digest != prev.get("sha256"):
        write_private(dest, r.stdout.decode())
        prev = {"sha256": digest, "changed_at": now()}
    return {**prev, "state": "ok", "checked_at": now()}


def sync_vault(state: dict) -> dict:
    prev = dict(state.get("vault", {}))
    r = ssh_main("export vault-index", timeout=60)
    idx = r.stdout.decode().strip() if not r.returncode else ""
    if idx == "off":
        return {"state": "off"}
    if not idx or idx == "unavailable":
        # Sealed after a restart, or down: keep the last copy and say so.
        return {**prev, "state": "unavailable", "note": "the main's vault did not answer (sealed?)"}
    if idx == prev.get("index") and (VAULT_COPY / "raft.snap").is_file():
        return {**prev, "state": "ok", "checked_at": now()}
    VAULT_COPY.mkdir(mode=0o700, exist_ok=True)
    snap = ssh_main("export vault", timeout=600)
    files = ssh_main("export bao-files", timeout=60)
    if snap.returncode or not snap.stdout or files.returncode:
        return {**prev, "state": "failed", "note": _err(snap if snap.returncode else files)}
    write_private(VAULT_COPY / "raft.snap.tmp", "")
    (VAULT_COPY / "raft.snap.tmp").write_bytes(snap.stdout)
    os.replace(VAULT_COPY / "raft.snap.tmp", VAULT_COPY / "raft.snap")
    for name, text in json.loads(files.stdout).items():
        write_private(VAULT_COPY / name.replace("/", "_"), text)
    return {"state": "ok", "index": idx, "copied_at": now(), "checked_at": now()}


def sync_identity(state: dict) -> dict:
    r = ssh_main("export identity", timeout=60)
    if r.returncode:
        return {**state.get("identity", {}), "state": "failed", "note": _err(r)}
    files = json.loads(r.stdout or b"{}")
    IDENTITY_COPY.mkdir(mode=0o700, exist_ok=True)
    for name in IDENTITY_FILES:
        if name in files:
            write_private(IDENTITY_COPY / name, files[name])
        else:
            (IDENTITY_COPY / name).unlink(missing_ok=True)
    missing = [n for n in IDENTITY_FILES[:1] if n not in files]
    return {"state": "ok", "files": sorted(files), "checked_at": now(),
            **({"note": "the main has no setup marker"} if missing else {})}


def _main_version() -> str:
    p = STATE / MAIN_CONFIG_COPY_NAME
    if not p.is_file():
        return ""
    m = re.search(r"^\s*version:\s*[\"']?([^\"'#\s]+)", p.read_text(), re.M)
    return m.group(1) if m else ""


def _writable(p: Path) -> bool:
    if p.exists():
        return os.access(p, os.W_OK) and os.access(p.parent, os.W_OK)
    q = p.parent
    while not q.exists():
        q = q.parent
    return os.access(q, os.W_OK)


def sync_code(state: dict) -> dict:
    """Follow the main's release: when its version differs from this server's,
    take the main's code (the exact tree it runs — works with any update source,
    and offline), then install its dependencies and images (`prepare`). The
    databases already carry the main's schema; nothing else is migrated here."""
    prev = dict(state.get("code", {}))
    want, have = _main_version(), version()
    last = prev.get("attempt_at")
    recent = bool(last) and (dt.datetime.now(dt.timezone.utc)
                             - dt.datetime.fromisoformat(last)).total_seconds() < 900
    if not want or want == have:
        if prev.get("state") != "failed":
            return {**prev, "state": "ok", "version": have}
        # The code arrived but `prepare` did not finish: still failed (a promote
        # would have to install everything at the worst moment). Retry it
        # every 15 minutes until it passes — the failure must not just vanish.
        if recent:
            return prev
        r = subprocess.run([str(ROOT / "standby-velaris.sh"), "prepare"], capture_output=True, text=True)
        if r.returncode:
            return {**prev, "attempt_at": now(),
                    "note": "`prepare` failed: " + (r.stderr or r.stdout).strip()[-160:]}
        return {**prev, "state": "ok", "attempt_at": now(), "note": None, "updated_at": now(),
                "version": have}
    if prev.get("target") == want and prev.get("state") == "failed" and recent:
        return prev                                  # failed recently: retry every 15 min, not each minute
    import tarfile
    res = {"target": want, "from": have, "attempt_at": now()}
    t = read_env(TUNNEL_ENV)
    proc = subprocess.Popen(["ssh", *_ssh_main_opts(t), f"{t['MAIN_USER']}@{t['MAIN_HOST']}", "export code"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    import shutil
    stage = STATE / "code-staging"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(mode=0o700)
    names: list[str] = []
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tar:
            for m in tar:
                # The same rule on this side: whatever the main sent, nothing that
                # is not code is written here.
                if not m.isfile() or not_code(os.path.normpath(m.name)) or m.name.startswith(("/", "..")):
                    continue
                tar.extract(m, stage, filter="data")
                names.append(os.path.normpath(m.name))
    except Exception as e:  # noqa: BLE001
        proc.kill()
        shutil.rmtree(stage, ignore_errors=True)
        return {**res, "state": "failed", "note": f"code copy failed: {e}"[:200]}
    proc.wait()
    if proc.returncode or not names:
        shutil.rmtree(stage, ignore_errors=True)
        return {**res, "state": "failed", "note": _err(proc) if proc.returncode else "empty code copy"}
    # Nothing replaced until everything can be: a copy failing half-way would
    # leave a tree of two releases (seen 2026-10-22: one file the owner could
    # not write). velaris.yaml — the version — goes last.
    blocked = [n for n in names if not _writable(ROOT / n)]
    if blocked:
        shutil.rmtree(stage, ignore_errors=True)
        return {**res, "state": "failed",
                "note": f"cannot write {len(blocked)} file(s) here, e.g. {blocked[0]} — fix ownership; "
                        "nothing was changed"}
    for name in sorted(names, key=lambda x: x == "velaris.yaml"):
        dst = ROOT / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage / name, dst)
    shutil.rmtree(stage, ignore_errors=True)
    n = len(names)
    r = subprocess.run([str(ROOT / "standby-velaris.sh"), "prepare"], capture_output=True, text=True)
    if r.returncode:
        return {**res, "state": "failed", "files": n, "version": version(),
                "note": "code updated; `prepare` failed: " + (r.stderr or r.stdout).strip()[-160:]}
    return {**res, "state": "ok", "files": n, "version": version(), "updated_at": now()}


def cmd_sync(a: argparse.Namespace | None = None) -> int:
    """velaris-standby-sync.timer, every 60 s after the last run finished."""
    if FENCE_ENV.exists() and not TUNNEL_ENV.exists():
        return fence_retry()
    if not TUNNEL_ENV.exists():
        return 0
    if taking_over_path().exists():
        return 0
    # The main first, quickly (step 12 (e), found on the boxes): a run against a
    # main that is gone spent half a minute in SSH timeouts while HOLDING the
    # lock an automatic promote waits for. A main that does not answer in 5 s
    # gets no copy this minute — and the lock is never taken.
    try:
        probe = ssh_main("export config", timeout=10, connect_timeout=5)
        reachable = probe.returncode == 0
    except (subprocess.TimeoutExpired, KeyError, OSError):
        reachable = False
    if not reachable:
        print("the main does not answer — no copy this minute", file=sys.stderr)
        return 0
    import fcntl
    private_dir()
    lock = os.fdopen(os.open(STATE / "sync.lock", os.O_WRONLY | os.O_CREAT, 0o600), "w")  # held for the run
    try:
        # The timer and a link's first copy (or a hand-run --allow-deletes) can
        # meet; two rsyncs into one folder must not.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another sync is running", file=sys.stderr)
        return 0
    state = json.loads(SYNC_STATE.read_text()) if SYNC_STATE.exists() else {}
    allow = bool(getattr(a, "allow_deletes", False))
    # Each part checks that no takeover has begun: a promote waits on this lock.
    parts = [("env", lambda: sync_file(state, "env", "export env", MAIN_ENV_COPY)),
             ("config", lambda: sync_file(state, "config", "export config", STATE / MAIN_CONFIG_COPY_NAME)),
             *[(n, (lambda n=n: sync_folder(n, state, allow))) for n in FOLDERS],
             ("vault", lambda: sync_vault(state)), ("identity", lambda: sync_identity(state)),
             ("code", lambda: sync_code(state))]
    report = {}
    for name, part in parts:
        if taking_over_path().exists():
            print("a takeover began — this copy stops here", file=sys.stderr)
            return 0
        report[name] = part()
    report["standby_version"] = version()
    state.update(report)
    write_private(SYNC_STATE, json.dumps(state, indent=2))
    try:
        ssh_main("report", data=json.dumps(report).encode(), timeout=60)
    except Exception:  # noqa: BLE001
        pass
    bad = [k for k, v in report.items()
           if isinstance(v, dict) and v.get("state") in ("failed", "deletes_held")]
    for k in bad:
        print(f"{k}: {report[k].get('note')}", file=sys.stderr)
    return 1 if bad else 0


# ── promote: the standby becomes the main (part c) ────────────────────────

def _replica_sql(key: str, sql: str, check: bool = True) -> str:
    name, user, db = DBS[key][4], DBS[key][1], DBS[key][2]
    r = run(["docker", "exec", "-i", name, "psql", "-U", user, "-d", db, "-p", setting(*DBS[key][5]),
             "-v", "ON_ERROR_STOP=1", "-tAX", "-f", "-"], input_text=sql, check=check)
    return r.stdout.strip()


def ssh_to(env_file: Path, request: str, timeout: int) -> subprocess.CompletedProcess:
    t = read_env(env_file)
    return subprocess.run(["ssh", *_ssh_main_opts(t), f"{t['MAIN_USER']}@{t['MAIN_HOST']}", request],
                          capture_output=True, timeout=timeout)


def fence_retry() -> int:
    """After `promote --main-is-down`: each minute, try to demote the old main.
    If it comes back (a reboot, a repaired network), its databases and gateway
    restart by themselves; this stops them within a minute."""
    rec = json.loads(PROMOTED.read_text()) if PROMOTED.exists() else {}
    try:
        r = ssh_to(FENCE_ENV, "demote", timeout=120)
        err = "" if (r.returncode == 0 and r.stdout.strip().startswith(b"{")) else _err(r)
    except subprocess.TimeoutExpired:
        err = "timed out"
    if err:
        # Still away — or back but no longer trusting this key (then check it by
        # hand: only `demote` or `retire` revoke it). Try again next minute.
        rec.update(fence_last_attempt=now(), fence_last_error=err)
        write_private(PROMOTED, json.dumps(rec, indent=2))
        if "denied" in err.lower():
            print("! the old main answers but refuses this server's key — check it is demoted",
                  file=sys.stderr)
        return 0
    final = json.loads(r.stdout).get("final_lsn") or {}
    rec.update(old_main_demoted_at=now(), old_main_final_lsn=final)
    # Writes the old main took after this server stopped receiving (it was cut
    # off, not down) are NOT here. Say so; they stay in its kept volumes.
    diverged = {k: v for k, v in final.items()
                if v and (rec.get("replayed_lsn") or {}).get(k)
                and _lsn(v) > _lsn(rec["replayed_lsn"][k]) + 256}   # its own shutdown record
    if diverged:
        rec["old_main_diverged"] = diverged
        print(f"! the old main took writes after the switch-over ({', '.join(diverged)}): they are "
              "NOT on this server; its volumes keep them — do not wipe it before checking",
              file=sys.stderr)
        subprocess.run(["logger", "-p", "user.warning", "-t", "velaris-standby",
                        f"old main diverged after promote: {diverged}"], capture_output=True)
    write_private(PROMOTED, json.dumps(rec, indent=2))
    for f in (FENCE_ENV, STATE / "tunnel_key", STATE / "tunnel_key.pub", STATE / "known_hosts"):
        f.unlink(missing_ok=True)
    print("the old main came back and was demoted", file=sys.stderr)
    return 0


def _lsn(text: str) -> int:
    hi, lo = text.split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


def _aside(stamp: str) -> Path:
    d = ROOT / ".backups" / f"before-promote-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT / ".backups", 0o700)
    os.chmod(d, 0o700)
    return d


def _install(src: Path, dst: Path, aside: Path) -> None:
    if dst.exists():
        os.replace(dst, aside / dst.name.lstrip("."))
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(src.read_bytes())


def missing_documents() -> tuple[int, list[str]]:
    """Every live document version's file, checked on disk (compose velaris-db up)."""
    raw = read_env(ROOT / ".env").get("VELARIS_CASE_STORAGE_BACKEND", "local").lower()
    if raw != "local":
        return 0, []
    base, _ = docs_source_for(read_env(ROOT / ".env"))
    cid = container_of("velaris-db")
    r = run(["docker", "exec", "-i", cid, "psql", "-U", "velaris", "-d", "velaris", "-tAX", "-c",
             "SELECT v.storage_key FROM document_versions v JOIN documents d ON d.id = v.document_id "
             "WHERE NOT coalesce(d.is_deleted, false)"], check=False)
    keys = [k for k in r.stdout.splitlines() if k.strip()]
    gone = [k for k in keys if ".." in Path(k).parts or not (base / k).is_file()]
    return len(keys), gone


def docs_source_for(env: dict) -> tuple[Path, str]:
    raw = env.get("VELARIS_CASE_STORAGE_LOCAL_PATH") or "./data/velaris-docs"
    return (Path(raw) if os.path.isabs(raw) else (ROOT / raw).resolve()), ""


STAGES = ("fenced", "databases", "files", "vault", "documents", "done")


def _stage(rec: dict, stage: str) -> None:
    rec["stage"] = stage
    write_private(PROMOTED, json.dumps(rec, indent=2))


def _promote_preflight(main_yaml: Path) -> None:
    """Everything that can stop a promote halfway is checked BEFORE the old main
    is fenced (seen 2026-10-22: no `jq` here left the vault sealed after the
    databases had already been promoted)."""
    import shutil
    need = ["docker", "ssh"] + (["jq"] if (VAULT_COPY / "raft.snap").is_file() else [])
    missing = [t for t in need if not shutil.which(t)]
    if missing:
        raise Refused(f"missing here: {', '.join(missing)} — install before promoting "
                      "(`standby-velaris.sh prepare` checks everything)")
    main_ver = ""
    if main_yaml.is_file():
        m = re.search(r"^\s*version:\s*[\"']?([^\"'#\s]+)", main_yaml.read_text(), re.M)
        main_ver = m.group(1) if m else ""
    if not MAIN_ENV_COPY.is_file() or not main_ver:
        raise Refused("this standby has no copy of the main's settings yet (.standby/main.env, "
                      "main.velaris.yaml) — it cannot run the platform. Check `sync`.")
    if main_ver != version():
        raise Refused(f"the main runs Velaris {main_ver}, this standby {version()} — install "
                      f"{main_ver} here first (the databases are already at {main_ver}'s schema)")
    if not (IDENTITY_COPY / ".velaris-setup-complete").is_file():
        raise Refused("no copy of the main's setup marker yet (.standby/identity/) — the platform "
                      "would refuse to start here. Check `sync`.")
    st = remote_status()
    for key, v in st["databases"].items():
        if not v.get("running") or not v.get("in_recovery"):
            raise Refused(f"the {key} replica is not running as a replica: {v}")


def _not_receiving() -> list[str]:
    return [k for k, v in remote_status()["databases"].items() if v.get("receiver") != "streaming"]


def cmd_promote(a: argparse.Namespace) -> int:
    """Stages, recorded in .standby/promoted.json: a promote stopped part-way
    (a crash, a failed vault open) is finished by running it again."""
    main_yaml = STATE / MAIN_CONFIG_COPY_NAME
    # A copy in progress (documents, code) must not keep writing into this
    # server while it becomes the main: wait for it, and hold the lock.
    import fcntl
    private_dir()
    lock = os.fdopen(os.open(STATE / "sync.lock", os.O_WRONLY | os.O_CREAT, 0o600), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("→ Waiting for the copy in progress to finish …")
        fcntl.flock(lock, fcntl.LOCK_EX)
    rec = json.loads(PROMOTED.read_text()) if PROMOTED.exists() else {}
    resuming = rec.get("stage") in STAGES[:-1]
    if rec.get("stage") == "done":
        raise Refused("this server has already been promoted")
    if not resuming:
        if not (STATE / "standby.json").exists():
            raise Refused("this server is not a linked standby")
        _promote_preflight(main_yaml)
        sync = json.loads(SYNC_STATE.read_text()) if SYNC_STATE.exists() else {}
        say("Promoting this standby to be the main server.")
        for k in ("env", "config", *FOLDERS, "vault"):
            v = sync.get(k) or {}
            say(f"  last copy  {k:10} {v.get('state')}  {v.get('synced_at') or v.get('checked_at') or ''}")
        behind = _not_receiving()
        if behind and not (a.main_is_down or a.accept_loss):
            # The main may well be up and holding writes this standby never got:
            # promoting now would throw them away without a word.
            raise Refused(f"this standby is not receiving ({', '.join(behind)}) — what the main wrote "
                          "since is not here. Fix replication (`status` on the main), or, knowing it "
                          "will be lost: --accept-loss (or --main-is-down if the main is gone)")
        if not a.yes and input("Type PROMOTE to continue: ").strip() != "PROMOTE":
            raise Refused("not confirmed")
        rec = {"promoted_at": now(), "from_version": version(), "main_reached": False,
               "old_main": read_env(TUNNEL_ENV).get("MAIN_HOST"),
               "stamp": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")}

        # 1. The old main must not take writes any more.
        final = None
        try:
            r = ssh_main("demote", timeout=180)
            if r.returncode == 0 and r.stdout.strip().startswith(b"{"):
                final = json.loads(r.stdout).get("final_lsn") or {}
                rec.update(main_reached=True, main_final_lsn=final)
                say("✓ The main answered: its gateway and databases are stopped, and it is marked demoted.")
        except subprocess.TimeoutExpired:
            pass
        if final is None:
            if not a.main_is_down:
                raise Refused("the main did not answer. If it is really down, run again with "
                              "--main-is-down — and keep it OFF (power it off or disconnect it): "
                              "until it is demoted it would still accept writes if anything reached it.")
            say("! The main did not answer (--main-is-down). This server will keep trying to demote "
                "it every minute, in case it comes back.")
            os.replace(TUNNEL_ENV, FENCE_ENV)          # the timer now runs fence_retry
        else:
            # Demoted and its key revoked: nothing left to reach it for.
            for f in (TUNNEL_ENV, STATE / "tunnel_key", STATE / "tunnel_key.pub", STATE / "known_hosts"):
                f.unlink(missing_ok=True)
        _stage(rec, "fenced")
    else:
        say(f"Resuming the promote after stage '{rec['stage']}'.")
        if rec["stage"] in ("fenced", "databases", "files"):
            _promote_preflight_tools_only()
    stamp = rec["stamp"]
    done = STAGES.index(rec["stage"])

    # 2. Everything the main sent, replayed; both databases leave recovery.
    if done < STAGES.index("databases"):
        final = rec.get("main_final_lsn") or {}
        end = time.monotonic() + 120
        for key in DBS:
            want = final.get(key)
            while want and time.monotonic() < end:
                if _replica_sql(key, f"SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '{want}') >= 0",
                                check=False) == "t":
                    break
                time.sleep(1)
            rec.setdefault("replayed_lsn", {})[key] = _replica_sql(key, "SELECT pg_last_wal_replay_lsn()")
            rec.setdefault("last_replayed_at", {})[key] = _replica_sql(
                key, "SELECT coalesce(pg_last_xact_replay_timestamp()::text, '')")
        short = {k: final[k] for k in DBS if final.get(k) and rec["replayed_lsn"].get(k)
                 and _lsn(rec["replayed_lsn"][k]) < _lsn(final[k])}
        if short and not getattr(a, "accept_loss", False):
            rec["short_of_main"] = short
            _stage(rec, "fenced")
            raise Refused(f"the main is stopped, but this standby did not receive all of it "
                          f"({', '.join(short)}: main ended at {short}, here "
                          f"{ {k: rec['replayed_lsn'][k] for k in short} }). Nothing is promoted yet. "
                          "Either bring the main back (on it: remove .standby/demoted.json, start it, "
                          "fix replication) or accept the loss: promote --accept-loss")
        run(["pkill", "-u", str(os.getuid()), "-f", f"[s]sh -N -T -i {STATE}/tunnel_key"], check=False)
        # The old main's reads key led to this replica — about to be the
        # writable main. It goes before the database leaves recovery.
        revoke_reads_key()
        for key in DBS:
            if _replica_sql(key, "SELECT pg_is_in_recovery()") == "t" and \
                    _replica_sql(key, "SELECT pg_promote(true, 60)") != "t":
                raise RuntimeError(f"{key}: pg_promote did not finish")
            # The main's address and the replication password leave the files.
            _replica_sql(key, "ALTER SYSTEM RESET primary_conninfo;\nALTER SYSTEM RESET primary_slot_name;")
            # The replication role came over from the main with a password the
            # standby held; a later `link` makes a new one.
            _replica_sql(key, f"DROP ROLE IF EXISTS {ROLE}")
        for key in DBS:
            run(["docker", "stop", "-t", "60", DBS[key][4]], check=False)
            run(["docker", "rm", "-f", DBS[key][4]], check=False)
        _stage(rec, "databases")
        say("✓ Both databases promoted.")

    # 3. The main's settings and configuration become this server's.
    if done < STAGES.index("files"):
        aside = _aside(stamp)
        _install(MAIN_ENV_COPY, ROOT / ".env", aside)
        # The main read from this server through ITS tunnel; here that address
        # leads nowhere (and a vault restored below would bring it back).
        if _read_url_is_ours(read_env(ROOT / ".env").get(READ_URL_KEY, "")):
            _env_set(ROOT / ".env", READ_URL_KEY, "")
            rec["read_url_cleared"] = True
        _install(main_yaml, ROOT / "velaris.yaml", aside)
        os.chmod(ROOT / "velaris.yaml", 0o644)
        for name in IDENTITY_FILES:
            if (IDENTITY_COPY / name).is_file():
                _install(IDENTITY_COPY / name, ROOT / name, aside)
        _stage(rec, "files")

    # 4. The vault, from the main's snapshot, opened with the main's keys.
    if done < STAGES.index("vault"):
        if (VAULT_COPY / "raft.snap").is_file():
            import importlib.util
            import shutil
            import tempfile
            spec = importlib.util.spec_from_file_location(
                "velaris_backup", ROOT / "scripts" / "backup" / "velaris_backup.py")
            vb = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(vb)
            with tempfile.TemporaryDirectory(dir=STATE) as x:
                xp = Path(x)
                (xp / "openbao").mkdir()
                shutil.copyfile(VAULT_COPY / "raft.snap", xp / "openbao.snap")
                for n in (".bao-init.json", "agent_role_id", "agent_secret_id"):
                    if (VAULT_COPY / n).is_file():
                        shutil.copyfile(VAULT_COPY / n, xp / "openbao" / n)
                say("→ Restoring the vault from the main's snapshot …")
                say(f"✓ Vault: {vb._restore_openbao(xp, stamp)}")       # noqa: SLF001
            (OPENBAO_DIR / "enabled").touch()
            if rec.get("read_url_cleared"):
                problem = _bao_patch({READ_URL_KEY: ""})
                if problem:
                    raise RuntimeError(f"the vault still holds the old main's read URL: {problem}")
        for f in (MAIN_ENV_COPY, main_yaml, SYNC_STATE, STATE / "standby.json", STATE / "sync.lock",
                  *(IDENTITY_COPY / n for n in IDENTITY_FILES)):
            f.unlink(missing_ok=True)
        if IDENTITY_COPY.is_dir():
            IDENTITY_COPY.rmdir()
        if VAULT_COPY.is_dir():
            for f in VAULT_COPY.iterdir():
                f.unlink()
            VAULT_COPY.rmdir()
        _stage(rec, "vault")

    # 5. The documents the database names, on disk?
    if done < STAGES.index("documents"):
        run(compose("up", "-d", "velaris-db", "temporal-db"))
        for _ in range(60):
            cid = container_of("velaris-db")
            if cid and run(["docker", "exec", cid, "pg_isready", "-U", "velaris", "-h", "127.0.0.1"],
                           check=False).returncode == 0:
                break
            time.sleep(1)
        total, gone = missing_documents()
        rec.update(documents_checked=total, documents_missing=len(gone), documents_missing_sample=gone[:20])
        _stage(rec, "documents")
        hxdr_record("promote", (read_env(FENCE_ENV) or {}).get("MAIN_HOST") or rec.get("old_main"),
                    {k: rec.get(k) for k in ("main_reached", "main_final_lsn", "replayed_lsn",
                                              "last_replayed_at", "short_of_main", "documents_checked",
                                              "documents_missing")}
                    | {"accepted_loss": bool(getattr(a, "accept_loss", False))})
        say(f"✓ Documents: {total - len(gone)} of {total} present"
            + (f" — {len(gone)} MISSING (written after the last copy; listed in {PROMOTED})"
               if gone else ""))

    # 6. Start.
    if a.no_start:
        _stage(rec, "done")
        say("Not started (--no-start). Start with ./start-velaris.sh (or the systemd units).")
    else:
        say("→ Starting the platform …")
        r = subprocess.run([str(ROOT / "start-velaris.sh")])
        if r.returncode:
            say("✗ start-velaris.sh failed (above). The databases are promoted; fix it and run "
                "./start-velaris.sh.")
            _stage(rec, "done")
            return 1
        _stage(rec, "done")
    say("✓ This server is now the main. Move users to it (DNS or floating IP — your step).")
    say("  To protect it again: link a new standby — `./standby-velaris.sh link <host>`.")
    say("  If this server has no platform units or backup timer yet: under systemd")
    say("  `sudo ./deploy/systemd/install.sh`; daily backups `sudo ./deploy/systemd/install-backup-timer.sh`.")
    if not rec["main_reached"]:
        say("  The old main was NOT reached: keep it off; when it is back this server demotes it.")
    return 0


def _promote_preflight_tools_only() -> None:
    import shutil
    need = ["docker"] + (["jq"] if (VAULT_COPY / "raft.snap").is_file() else [])
    missing = [t for t in need if not shutil.which(t)]
    if missing:
        raise Refused(f"missing here: {', '.join(missing)} — install, then run promote again")


def _missing_packages() -> list[str]:
    """The platform's own dependency list (deploy/deps/velaris-deps.sh, the one
    setup installs from) plus what a standby needs on top: jq (vault), the SSH
    client (the link), uv (setup installs it from astral.sh, not apt)."""
    import shutil
    os.environ["PATH"] = f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}"
    deps = (ROOT / "deploy" / "deps" / "velaris-deps.sh").read_text()
    block = re.search(r'VELARIS_REQUIRED_COMMANDS="\\\n(.*?)"', deps, re.S)
    pairs = [ln.split(":")[:2] for ln in (block.group(1).splitlines() if block else []) if ":" in ln]
    pairs += [["jq", "jq"], ["ssh", "openssh-client"], ["uv", "uv"]]
    return [pkg for cmd, pkg in pairs if not shutil.which(cmd)]


def cmd_prepare(_a: argparse.Namespace) -> int:
    """On a server that will be a standby: everything a promote would otherwise
    have to download at the worst moment — dependencies and images."""
    import shutil
    pkgs = _missing_packages()
    if pkgs and getattr(_a, "install_deps", False):
        say(f"→ Installing: {' '.join(pkgs)} (sudo apt-get) …")
        subprocess.run(["sudo", "apt-get", "update", "-qq"], check=True)
        subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", *[p for p in pkgs if p != "uv"]],
                       check=True)
        if "uv" in pkgs:   # setup-velaris.sh installs it the same way
            subprocess.run(["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], check=True)
            os.environ["PATH"] = f"{Path.home()}/.local/bin:{os.environ['PATH']}"
        pkgs = _missing_packages()
    if pkgs:
        raise Refused(f"missing here (apt packages): {' '.join(pkgs)} — run "
                      "`./standby-velaris.sh prepare --install-deps` (sudo), or install them yourself")
    if (ROOT / ".env").exists() and not (STATE / "standby.json").exists() and not PREPARED.exists():
        raise Refused("this folder holds an install (.env) — prepare a fresh copy of the release")
    say("→ Python dependencies (uv sync) …")
    run(["uv", "sync", "--all-packages", "--quiet"])
    say("→ Studio dependencies (npm install) …")
    subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=ROOT / "studio",
                   capture_output=True, check=True)
    say("→ Container images …")
    env = dict(os.environ, VELARIS_DB_PASSWORD="x")      # compose needs it to read the file
    images = run(["docker", "compose", "-f", str(COMPOSE_FILE), "--profile", "*", "config", "--images"],
                 env=env).stdout.split()
    failed = [i for i in sorted(set(images)) if not docker_ok("image", "inspect", i)
              and run(["docker", "pull", "-q", i], check=False).returncode]
    write_private(PREPARED, json.dumps({"version": version(), "at": now(), "images_missing": failed},
                                       indent=2))
    for i in failed:
        say(f"! could not pull {i}")
    say("✓ Prepared." + (" (some images missing — a promote would need them)" if failed else ""))
    return 1 if failed else 0


def remote_sync(_p: dict) -> dict:
    rc = cmd_sync()
    return {"sync_rc": rc, **json.loads(SYNC_STATE.read_text())}


REMOTE = {"remote-sync": remote_sync, "remote-has-volume": remote_has_volume,
          "remote-check": remote_check, "remote-key": remote_key, "remote-start": remote_start,
          "remote-status": remote_status, "remote-stop": remote_stop,
          "remote-reads-allow": remote_reads_allow, "remote-reads-revoke": remote_reads_revoke}


class _ThisModule:
    """This module's own globals, read live — so the failover module works on
    exactly the state this copy works on (a script run, or a test's isolated
    copy with its paths in a throwaway install), never on another import."""

    def __init__(self, g: dict) -> None:
        self.__dict__["_g"] = g

    def __getattr__(self, name: str):
        try:
            return self._g[name]
        except KeyError:
            raise AttributeError(name) from None


def _failover():
    """Step 12's commands live beside this tool (velaris_failover.py)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import velaris_failover
    velaris_failover.sb = _ThisModule(globals())
    return velaris_failover


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].startswith("remote-failover-"):
        REMOTE.update(_failover().REMOTE)
    if argv and argv[0] in REMOTE:
        # Called over SSH: input is JSON on stdin, the answer is one JSON line.
        try:
            payload = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
            print(json.dumps({"ok": True, **REMOTE[argv[0]](payload)}), flush=True)
            return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(e)}), flush=True)
            return 1

    ap = argparse.ArgumentParser(prog="standby-velaris.sh", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lk = sub.add_parser("link", help="link a second server as this one's standby")
    lk.add_argument("host", help="[user@]standby-host (reached with your own SSH)")
    lk.add_argument("--ssh-port", type=int, default=22, help="the standby's SSH port")
    lk.add_argument("--standby-dir", help="Velaris folder on the standby (default: same path as here)")
    lk.add_argument("--main-address", help="this server's address as the standby reaches it "
                    "(default: the one its SSH session came from)")
    lk.add_argument("--main-ssh-port", type=int, default=22, help="this server's SSH port")
    lk.add_argument("--replace-standby-data", action="store_true",
                    help="discard a database the standby already holds")
    lk.add_argument("--skip-models", action="store_true", help="do not copy Ollama's models")
    st = sub.add_parser("status", help="replication state (writes .standby/status.json)")
    st.add_argument("--json", action="store_true")
    ul = sub.add_parser("unlink", help="stop being linked; the main keeps running")
    ul.add_argument("--local-only", action="store_true", help="do not contact the standby")
    sub.add_parser("serve", help=argparse.SUPPRESS)
    sr = sub.add_parser("serve-reads", help="(main) send dashboards, reports and search to the standby's copy")
    sr.add_argument("action", choices=("on", "off", "status"))
    pr = sub.add_parser("promote", help="(standby) become the main server")
    pr.add_argument("--main-is-down", action="store_true",
                    help="the main does not answer; promote anyway (keep it OFF)")
    pr.add_argument("--yes", action="store_true", help="do not ask to confirm")
    pr.add_argument("--accept-loss", action="store_true",
                    help="promote although this standby is behind the main (its writes are lost)")
    pr.add_argument("--no-start", action="store_true", help="promote, but do not start the platform")
    sub.add_parser("retire", help="(old main, after a promote) clear it to rejoin as a standby")
    pp = sub.add_parser("prepare", help="(future standby) install dependencies and images in advance")
    pp.add_argument("--install-deps", action="store_true",
                    help="install missing system packages and uv (sudo), as setup does")
    sy = sub.add_parser("sync", help="(standby) pull documents, settings and vault — run by its timer")
    sy.add_argument("--allow-deletes", action="store_true",
                    help="mirror deletions the guard held back (after checking the main)")
    fo = _failover()
    fo.add_parser(sub)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "failover":
            return fo.run_command(a)
        return {"link": cmd_link, "status": cmd_status, "unlink": cmd_unlink,
                "serve": cmd_serve, "sync": cmd_sync, "promote": cmd_promote,
                "retire": cmd_retire, "prepare": cmd_prepare, "serve-reads": cmd_serve_reads}[a.cmd](a)
    except Refused as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
