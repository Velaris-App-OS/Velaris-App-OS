#!/usr/bin/env python3
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
"""Velaris backup engine — one encrypted, self-describing backup set.

Design: docs/Future/Done/backup-restore-design.md (Nov_TODO R5a). Called by
`backup-velaris.sh`; restore lives beside it (step b).

A set is ONE file, ``velaris-<host>-<UTC stamp>-v<version>.tar.age``:

    manifest.json      version, migration level, row counts, file SHA-256s, times
    database.*         pg_dump -Fc (PostgreSQL) or mysqldump (MySQL / MariaDB)
    documents.tar      the local document store, or the bundled MinIO volume
    env                the .env — holds VELARIS_CASE_KEK and the storage master key
    openbao.snap       `bao operator raft snapshot save`
    openbao/…          .bao-init.json and the agent's AppRole files

encrypted to the operator's age public key (VELARIS_BACKUP_AGE_RECIPIENT). The
private key is never on this path. Beside it, an UNENCRYPTED ``.json`` sidecar
lists what the set holds (no secrets) so retention and listing never decrypt.

Without a recipient the run falls back to what backups always were — a
plain database dump, local only — because settings and the vault must never
be written to an unencrypted set.

Run it with the repo venv (it needs `minio` only for an S3 destination):

    .venv/bin/python scripts/backup/velaris_backup.py backup [--quiet]
    .venv/bin/python scripts/backup/velaris_backup.py ensure-key
    .venv/bin/python scripts/backup/velaris_backup.py forget-private-key
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy" / "docker-compose" / "docker-compose.yml"
REPORT = ROOT / "velaris-install-report.md"
SET_RE = re.compile(r"^velaris-(?P<host>[A-Za-z0-9._-]+)-(?P<stamp>\d{8}T\d{6}Z)-v(?P<ver>[0-9][0-9A-Za-z.\-]*)\.tar\.age$")
KEY_BEGIN = "<!-- velaris-backup-private-key:begin -->"
KEY_END = "<!-- velaris-backup-private-key:end -->"
# Client images for a bring-your-own MySQL / MariaDB (no DB container here).
# Pinned, like every image (scripts/check-image-versions.sh).
MYSQL_CLIENT_IMAGE = "mysql:8.4.11"
MARIADB_CLIENT_IMAGE = "mariadb:11.8.9"
# Used only to read a Docker volume as a tar stream (already pinned for egress-gw).
VOLUME_READER_IMAGE = "python:3.12.15-alpine"
# psql / pg_dump for a PostgreSQL on another server (the bundled one's own image).
PG_CLIENT_IMAGE = "postgres:16.15-alpine"
EXTERNAL_PARTS = {"database", "temporal", "ai", "meetings", "events"}

QUIET = False


def say(msg: str) -> None:
    if not QUIET:
        print(msg, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── configuration ─────────────────────────────────────────────────────────

def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of .env. Values are taken literally (quotes stripped)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def yaml_get(key: str) -> str:
    """One scalar from velaris.yaml (flat lookup, like setup-velaris.sh's _yaml_get)."""
    p = ROOT / "velaris.yaml"
    if not p.is_file():
        return ""
    for line in p.read_text(errors="replace").splitlines():
        m = re.match(rf"^\s*{re.escape(key)}:\s*[\"']?([^\"'#]*)[\"']?", line)
        if m:
            return m.group(1).strip()
    return ""


def truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


class Config:
    def __init__(self) -> None:
        self.env = read_env(ROOT / ".env")
        # VELARIS_BACKUP_* in the environment win over .env (a timer unit, a
        # one-off run to another disk). Nothing else is taken from the environment.
        self.env.update({k: v for k, v in os.environ.items() if k.startswith("VELARIS_BACKUP_")})
        e = self.env
        self.version = yaml_get("version") or "0.0.0"
        backend = (e.get("VELARIS_CASE_DATABASE_BACKEND") or yaml_get("database") or "postgresql").lower()
        self.db_backend = backend if backend in {"postgresql", "mysql", "mariadb"} else "postgresql"
        self.local_dir = Path(e.get("VELARIS_BACKUP_DIR") or (ROOT / ".backups"))
        self.nas_dir = Path(e["VELARIS_BACKUP_PATH"]) if e.get("VELARIS_BACKUP_PATH") else None
        self.s3 = {k: e.get(f"VELARIS_BACKUP_S3_{k.upper()}", "")
                   for k in ("endpoint", "bucket", "access_key", "secret_key", "region", "prefix")}
        self.s3_secure = not (e.get("VELARIS_BACKUP_S3_SECURE", "true").lower() in {"0", "false", "no", "off"})
        self.recipient = e.get("VELARIS_BACKUP_AGE_RECIPIENT", "").strip()
        self.keep = tuple(int(e.get(f"VELARIS_BACKUP_KEEP_{k}", d) or d)
                          for k, d in (("DAILY", 7), ("WEEKLY", 4), ("MONTHLY", 6)))
        self.storage_backend = (e.get("VELARIS_CASE_STORAGE_BACKEND") or "local").lower()
        lp = e.get("VELARIS_CASE_STORAGE_LOCAL_PATH") or "./data/velaris-docs"
        self.storage_local_path = (ROOT / lp).resolve() if not os.path.isabs(lp) else Path(lp)
        self.openbao_enabled = (ROOT / "deploy" / "openbao" / "enabled").is_file()
        self.host = re.sub(r"[^A-Za-z0-9.-]", "-", socket.gethostname())[:40] or "host"
        # Parts on other servers (deploy/external-services.sh). Unset = all bundled.
        raw = os.environ.get("VELARIS_EXTERNAL_SERVICES") or e.get("VELARIS_EXTERNAL_SERVICES", "")
        self.external = {w for w in re.split(r"[,\s]+", raw.lower()) if w in EXTERNAL_PARTS}
        self.pg_url = _libpq_url(e.get("VELARIS_CASE_DATABASE_URL", ""))
        self.pg_client_image = (os.environ.get("VELARIS_PG_CLIENT_IMAGE")
                                or e.get("VELARIS_PG_CLIENT_IMAGE") or PG_CLIENT_IMAGE)


# ── small helpers ─────────────────────────────────────────────────────────

def run(cmd: list[str], *, stdout=None, input_bytes: bytes | None = None,
        env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=stdout if stdout is not None else subprocess.PIPE,
                          stderr=subprocess.PIPE, input=input_bytes, env=env, check=check)


_LIBPQ_KEYS = {"sslmode", "sslrootcert", "sslcert", "sslkey", "sslcrl", "sslsni",
               "application_name", "connect_timeout", "options", "target_session_attrs"}


def _libpq_url(url: str) -> str:
    """postgresql+asyncpg://…?ssl=verify-full → postgresql://…?sslmode=verify-full&sslrootcert=system
    (the platform's URL, in the form psql and pg_dump read — the same rules as
    deploy/external-services.sh: libpq's own options only, since a driver
    option makes psql refuse the URL; verify-full without a CA file = the
    system's CAs, which is what the platform checks against)."""
    url = re.sub(r"^(postgres(?:ql)?)\+[a-z0-9_]+://", r"\1://", url)
    base, sep, q = url.partition("?")
    keep: list[str] = []
    mode = root = ""
    for kv in filter(None, q.split("&")):
        k, _, v = kv.partition("=")
        if k == "ssl":
            k = "sslmode"
        if k in _LIBPQ_KEYS:
            keep.append(f"{k}={v}")
        mode = v if k == "sslmode" else mode
        root = v if k == "sslrootcert" else root
    if mode == "verify-full" and not root:
        keep.append("sslrootcert=system")
    return base + ("?" + "&".join(keep) if keep else "")


def pg_cmd(cfg: "Config", tool: str, *args: str, interactive: bool = False,
           cid: str | None = None) -> tuple[list[str], dict | None]:
    """(argv, env) running a PostgreSQL client tool against the platform database.

    Bundled: `docker exec` into the container, as before. On another server: a
    pinned client container on the host network; the URL (with its password)
    travels in the environment, never on a command line, and the certificate
    files it names are mounted read-only at the same paths."""
    if "database" not in cfg.external:
        return (["docker", "exec"] + (["-i"] if interactive else []) + [cid or "", tool,
                "-U", "velaris", "-d", "velaris", *args], None)
    mounts: list[str] = []
    q = cfg.pg_url.split("?", 1)[1] if "?" in cfg.pg_url else ""
    for kv in q.split("&"):
        k, _, v = kv.partition("=")
        if k in {"sslrootcert", "sslcert", "sslkey"} and v and v != "system":
            mounts += ["-v", f"{v}:{v}:ro"]
    # The password leaves the URL for PGPASSWORD: inside the container the URL
    # is the tool's own argument, and that command line is visible to `ps` on
    # the host.
    from urllib.parse import unquote, urlsplit
    parts = urlsplit(cfg.pg_url)
    url, password = cfg.pg_url, ""
    if parts.password is not None:
        password = unquote(parts.password)
        netloc = f"{parts.username or ''}@{parts.netloc.rsplit('@', 1)[1]}"
        url = parts._replace(netloc=netloc).geturl()
    argv = ["docker", "run", "--rm", "-i", "--network", "host", "-e", "VELARIS_PGURL", "-e", "PGPASSWORD",
            *mounts, getattr(cfg, "pg_client_image", PG_CLIENT_IMAGE), "sh", "-c", 't="$1"; shift; exec "$t" -d "$VELARIS_PGURL" "$@"',
            "sh", tool, *args]
    return argv, {**os.environ, "VELARIS_PGURL": url, "PGPASSWORD": password}


def compose(*args: str) -> list[str]:
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if (ROOT / ".env").is_file():
        cmd += ["--env-file", str(ROOT / ".env")]
    return cmd + list(args)


def _recreate_containers(cfg: "Config") -> str:
    """The containers took their secrets from .env when they were created — the
    gateway's identity secret, the cache's and the broker's passwords. A restore
    brings the backed-up install's .env, so they are recreated from it, as
    start-velaris.sh does (same profiles, same service list). Found in the R5b
    drill (2026-10-30): on a new server the gateway kept the new install's
    secret and the services refused every request (VEL-SYS-GATEWAY-REQUIRED-403).
    Never fatal: the data is already restored; a failure names the command."""
    script = ('. "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"; S=""; '
              'if type velaris_compose_up_services >/dev/null 2>&1; then '
              'S="$(velaris_compose_up_services "$1" "$2")" || exit 1; fi; '
              'docker compose -f "$1" --env-file "$2" up -d $S')
    r = subprocess.run(["bash", "-c", script, "_", str(COMPOSE_FILE), str(ROOT / ".env")],
                       env={**os.environ, "VELARIS_DIR": str(ROOT)}, capture_output=True, text=True)
    if r.returncode:
        return ("NOT recreated from the restored settings — before starting: "
                "docker compose -f deploy/docker-compose/docker-compose.yml --env-file .env up -d  "
                f"({(r.stderr or r.stdout).strip()[-160:]})")
    if cfg.openbao_enabled:
        # A recreated OpenBao comes back sealed.
        subprocess.run([str(ROOT / "scripts" / "secrets-unseal.sh")], capture_output=True)
    return "recreated from the restored settings"


def compose_project() -> str:
    try:
        out = run(compose("config", "--format", "json")).stdout
        return json.loads(out)["name"]
    except Exception:
        return "docker-compose"


def container_of(service: str) -> str | None:
    try:
        cid = run(compose("ps", "-q", service)).stdout.decode().strip()
        return cid or None
    except Exception:
        return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


# ── parts of a set ────────────────────────────────────────────────────────

_PG_COUNTS_SQL = (
    "SELECT table_name || '\t' || "
    "(xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', "
    "table_schema, table_name), false, true, '')))[1]::text "
    "FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1;"
)


def dump_postgres(cfg: Config, work: Path, manifest: dict) -> Path:
    """pg_dump in the SAME snapshot the row counts are read in.

    A psql session exports a REPEATABLE READ snapshot and stays open; pg_dump
    runs with --snapshot, and the counts are then read through that same
    session — so the manifest's counts are exactly what the dump holds, on a
    live system, without stopping anything.
    """
    external = "database" in cfg.external
    cid = None if external else container_of("velaris-db")
    if not external and not cid:
        raise RuntimeError("the velaris-db container is not running")
    if external and not cfg.pg_url:
        raise RuntimeError("VELARIS_CASE_DATABASE_URL is not set")
    argv, env = pg_cmd(cfg, "psql", "-At", "-q", "-v", "ON_ERROR_STOP=1", interactive=True, cid=cid)
    sess = subprocess.Popen(argv, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert sess.stdin and sess.stdout
        sess.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();\n")
        sess.stdin.flush()
        snap = sess.stdout.readline().strip()
        if not re.fullmatch(r"[0-9A-F-]+", snap or ""):
            raise RuntimeError(f"could not export a snapshot ({snap!r})")
        out = work / "database.pgdump"
        manifest["database"]["dump_started_at"] = utcnow().isoformat()
        argv, env = pg_cmd(cfg, "pg_dump", "-Fc", f"--snapshot={snap}", cid=cid)
        with out.open("wb") as f:
            r = subprocess.run(argv, env=env, stdout=f, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError("pg_dump failed: " + r.stderr.decode(errors="replace")[-400:])
        sess.stdin.write(_PG_COUNTS_SQL + "\nSELECT '__END__';\n")
        sess.stdin.flush()
        counts: dict[str, int] = {}
        for line in sess.stdout:
            line = line.strip()
            if line == "__END__":
                break
            if "\t" in line:
                t, c = line.split("\t", 1)
                counts[t] = int(c or 0)
        argv, env = pg_cmd(cfg, "psql", "-At", "-c",
                           "SELECT count(*) || '\t' || coalesce(max(filename),'') FROM schema_migrations;",
                           cid=cid)
        mig = run(argv, env=env, check=False).stdout.decode().strip()
        manifest["database"].update({
            "backend": "postgresql", "format": "pg_dump custom", "file": out.name,
            "source": "external" if external else "bundled",
            "snapshot_consistent_counts": True, "row_counts": counts,
            "migrations": _mig(mig), "dump_finished_at": utcnow().isoformat(),
        })
        return out
    finally:
        try:
            if sess.stdin:
                sess.stdin.write("COMMIT;\n\\q\n")
                sess.stdin.close()
        except Exception:
            pass
        try:
            sess.wait(timeout=30)
        except Exception:
            sess.kill()


def _mig(line: str) -> dict:
    n, _, last = line.partition("\t")
    try:
        return {"applied": int(n), "last": last}
    except ValueError:
        return {"applied": None, "last": None}


def dump_mysql(cfg: Config, work: Path, manifest: dict) -> Path:
    """mysqldump --single-transaction from a pinned client container.

    MySQL and MariaDB are bring-your-own here (no DB container), so the client
    runs with host networking against the operator's server. Row counts are
    read in a separate consistent-snapshot transaction, not the dump's own, so
    on a busy system they can differ slightly — marked as such.
    """
    e = cfg.env
    host = e.get("VELARIS_CASE_DB_HOST") or yaml_get("db_host") or "127.0.0.1"
    port = e.get("VELARIS_CASE_DB_PORT") or yaml_get("db_port") or "3306"
    name = e.get("VELARIS_CASE_DB_NAME") or yaml_get("db_name") or "velaris"
    user = e.get("VELARIS_CASE_DB_USER") or yaml_get("db_user") or "velaris"
    pw = e.get("VELARIS_DB_PASSWORD") or e.get("VELARIS_CASE_DB_PASSWORD") or ""
    maria = cfg.db_backend == "mariadb"
    image, dumper, client = ((MARIADB_CLIENT_IMAGE, "mariadb-dump", "mariadb") if maria
                             else (MYSQL_CLIENT_IMAGE, "mysqldump", "mysql"))
    # TLS when the URL names a CA (?ssl_ca=…): verified, hostname included.
    ca = re.search(r"[?&]ssl_ca=([^&]+)", e.get("VELARIS_CASE_DATABASE_URL", ""))
    mounts = ["-v", f"{ca.group(1)}:{ca.group(1)}:ro"] if ca else []
    tls = ([] if not ca else
           ["--ssl", f"--ssl-ca={ca.group(1)}", "--ssl-verify-server-cert"] if maria else
           ["--ssl-mode=VERIFY_IDENTITY", f"--ssl-ca={ca.group(1)}"])
    base = ["docker", "run", "--rm", "-i", "--network", "host", "-e", "MYSQL_PWD", *mounts, image]
    env = {**os.environ, "MYSQL_PWD": pw}
    out = work / "database.sql"
    manifest["database"]["dump_started_at"] = utcnow().isoformat()
    with out.open("wb") as f:
        r = subprocess.run(base + [dumper, "-h", host, "-P", str(port), "-u", user, *tls,
                                   "--single-transaction", "--routines", "--triggers", "--events",
                                   "--hex-blob", "--default-character-set=utf8mb4", name],
                           stdout=f, stderr=subprocess.PIPE, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{dumper} failed: " + r.stderr.decode(errors="replace")[-400:])
    q = ("SELECT table_name FROM information_schema.tables "
         f"WHERE table_schema = '{name}' AND table_type = 'BASE TABLE' ORDER BY 1;")
    tables = run(base + [client, "-h", host, "-P", str(port), "-u", user, *tls, "-N", "-B", "-e", q],
                 env=env, check=False).stdout.decode().split()
    counts: dict[str, int] = {}
    if tables:
        sql = "START TRANSACTION WITH CONSISTENT SNAPSHOT; " + " ".join(
            f"SELECT '{t}', COUNT(*) FROM `{name}`.`{t}`;" for t in tables) + " COMMIT;"
        res = run(base + [client, "-h", host, "-P", str(port), "-u", user, *tls, "-N", "-B", "-e", sql],
                  env=env, check=False).stdout.decode()
        for line in res.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].isdigit():
                counts[parts[0]] = int(parts[1])
    # -r: raw output — in batch mode the client escapes the tab to a literal "\t".
    mig = run(base + [client, "-h", host, "-P", str(port), "-u", user, *tls, "-N", "-B", "-r", "-e",
                      f"SELECT CONCAT(COUNT(*), CHAR(9), COALESCE(MAX(filename),'')) FROM `{name}`.schema_migrations;"],
              env=env, check=False).stdout.decode().strip()
    manifest["database"].update({
        "backend": cfg.db_backend, "format": f"{dumper} (single transaction)", "file": out.name,
        "snapshot_consistent_counts": False, "row_counts": counts,
        "migrations": _mig(mig), "dump_finished_at": utcnow().isoformat(),
    })
    return out


def _probe(before: str | None = None) -> dict:
    """Chain states and a few readable documents, right after the dump, so a
    restore is checked against what was there (verify_restore.py --probe).
    Best-effort: a probe that cannot run leaves the set valid, just checked less.
    Directly on the database, as the dump is: the pooler is down during a
    restore's safety backup (clean box 2026-10-23: "probe skipped")."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "services" / "case-service"),
                                                         str(ROOT / "services" / "_shared")]),
           "VELARIS_CASE_DATABASE_POOLER_URL": "", "VELARIS_CASE_DATABASE_POOLER_SOCKET": "false"}
    try:
        r = subprocess.run([str(ROOT / ".venv" / "bin" / "python"),
                            str(ROOT / "scripts" / "backup" / "verify_restore.py"), "--probe"]
                           + ([before] if before else []),
                           cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        return json.loads(r.stdout.decode().strip().splitlines()[-1])
    except Exception as exc:
        warn(f"  ! probe skipped ({type(exc).__name__}) — restore will be checked against ideals")
        return {}


def capture_documents(cfg: Config, work: Path, manifest: dict) -> Path | None:
    """The document store, taken right after the dump (the manifest has both times)."""
    out = work / "documents.tar"
    doc = manifest["documents"]
    doc["started_at"] = utcnow().isoformat()
    if cfg.storage_backend == "local":
        src = cfg.storage_local_path
        if not src.is_dir():
            doc.update({"included": True, "source": "local", "note": "store directory does not exist yet (no documents)"})
            with tarfile.open(out, "w"):
                pass
        else:
            with tarfile.open(out, "w") as t:
                t.add(str(src), arcname="documents")
            doc.update({"included": True, "source": "local", "path": str(src)})
    else:
        # The bundled MinIO keeps its objects in a Docker volume on THIS host;
        # any other S3 endpoint is the operator's own service and its own backup.
        vol = f"{compose_project()}_velaris-minio-data"
        bundled = run(["docker", "volume", "inspect", vol], check=False).returncode == 0
        endpoint = (cfg.env.get("VELARIS_CASE_S3_ENDPOINT") or cfg.env.get("VELARIS_CASE_MINIO_ENDPOINT") or "")
        if bundled and (not endpoint or endpoint.split(":")[0] in {"127.0.0.1", "localhost", "minio", "velaris-minio"}):
            with out.open("wb") as f:
                r = subprocess.run(["docker", "run", "--rm", "-v", f"{vol}:/v:ro", VOLUME_READER_IMAGE,
                                    "tar", "-C", "/v", "-cf", "-", "."], stdout=f, stderr=subprocess.PIPE)
            if r.returncode != 0:
                raise RuntimeError("reading the MinIO volume failed: " + r.stderr.decode(errors="replace")[-300:])
            doc.update({"included": True, "source": "bundled-minio", "volume": vol})
        else:
            doc.update({"included": False, "source": "external-s3", "endpoint": endpoint,
                        "note": "documents live in the operator's own S3 service — back that up there"})
            doc["finished_at"] = utcnow().isoformat()
            return None
    doc["finished_at"] = utcnow().isoformat()
    return out


TEMPORAL_DBS = ("temporal", "temporal_visibility")


def capture_temporal(cfg: Config, work: Path, manifest: dict) -> list[Path]:
    """Temporal's own databases: the in-flight workflows (SLA timers,
    escalations). Without them a restored install has every open case's timers
    silently stopped — the case rows come back, the workflows watching them do
    not. Taken right after the platform dump; not the same snapshot, which the
    workflows tolerate by design: every activity re-reads the case row before it
    acts (temporal/activities, verify-before-fire)."""
    info = manifest["temporal"] = {"started_at": utcnow().isoformat()}
    if "temporal" in cfg.external:
        # Its databases are on the Temporal server, not here: backed up there.
        info.update({"included": False, "external": True,
                     "note": "held externally — back it up there"})
        return []
    cid = container_of("temporal-db")
    if not cid:
        info.update({"included": False, "note": "temporal-db container not running"})
        return []
    out: list[Path] = []
    for db in TEMPORAL_DBS:
        f = work / f"{db}.pgdump"
        with f.open("wb") as fh:
            r = subprocess.run(["docker", "exec", cid, "pg_dump", "-U", "temporal", "-d", db, "-Fc"],
                               stdout=fh, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError(f"pg_dump {db}: " + r.stderr.decode(errors="replace")[-300:])
        out.append(f)
    info.update({"included": True, "databases": list(TEMPORAL_DBS), "finished_at": utcnow().isoformat()})
    return out


def capture_settings(cfg: Config, work: Path, manifest: dict) -> list[Path]:
    files = []
    env = ROOT / ".env"
    if env.is_file():
        dst = work / "env"
        shutil.copyfile(env, dst)
        dst.chmod(0o600)
        files.append(dst)
    manifest["settings"] = {"env_included": bool(files)}
    return files


def capture_openbao(cfg: Config, work: Path, manifest: dict) -> list[Path]:
    info = manifest["openbao"]
    if not cfg.openbao_enabled:
        info.update({"included": False, "note": "OpenBao not enabled on this install"})
        return []
    files: list[Path] = []
    init = ROOT / "deploy" / "openbao" / ".bao-init.json"
    sub = work / "openbao"
    sub.mkdir(mode=0o700)
    for name in (".bao-init.json", "agent/role_id", "agent/secret_id"):
        p = ROOT / "deploy" / "openbao" / name
        if p.is_file():
            d = sub / name.replace("/", "_")
            shutil.copyfile(p, d)
            d.chmod(0o600)
            files.append(d)
    try:
        token = json.loads(init.read_text())["root_token"]
    except Exception:
        info.update({"included": False, "note": "no root token (.bao-init.json) — snapshot not taken"})
        return files
    # After a reboot OpenBao comes back sealed, and the timer can fire before
    # anything has unsealed it (2026-10-22 and 10-23: two partial backups).
    # Unseal from the local keyfile first — a no-op when already unsealed,
    # never prints key material; if it cannot, the snapshot below fails and
    # says so, as before.
    unseal = ROOT / "scripts" / "secrets-unseal.sh"
    if unseal.is_file():
        subprocess.run(["bash", str(unseal)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, check=False)
    snap = work / "openbao.snap"
    # The token goes in on stdin, never on a command line (`ps` shows those to
    # every local user).
    with snap.open("wb") as fh:
        r = subprocess.run(["docker", "exec", "-i", "-e", "BAO_ADDR=http://127.0.0.1:8200",
                            "velaris-openbao", "sh", "-c",
                            "read -r BAO_TOKEN; export BAO_TOKEN; "
                            "bao operator raft snapshot save /tmp/velaris-backup.snap >/dev/null && "
                            "cat /tmp/velaris-backup.snap; rc=$?; rm -f /tmp/velaris-backup.snap; exit $rc"],
                           input=(token + "\n").encode(), stdout=fh, stderr=subprocess.PIPE)
    if r.returncode != 0 or snap.stat().st_size == 0:
        snap.unlink(missing_ok=True)
        info.update({"included": False,
                     "note": "raft snapshot failed (sealed?): " + r.stderr.decode(errors="replace")[-200:]})
        return files
    snap.chmod(0o600)
    info.update({"included": True, "snapshot": snap.name})
    return files + [snap]


# ── proving a set restores (before it is encrypted) ───────────────────────

RESTORE_TEST_STATE = ".restore-test.json"
TEST_PG_IMAGE = "postgres:16.15-alpine"


def _restore_test_due(cfg: Config, force: bool | None) -> bool:
    if force is not None:
        return force
    every = int(cfg.env.get("VELARIS_BACKUP_TEST_EVERY_DAYS") or 7)
    try:
        last = json.loads((cfg.local_dir / RESTORE_TEST_STATE).read_text())["last_passed_at"]
        return utcnow() - dt.datetime.fromisoformat(last) >= dt.timedelta(days=every)
    except Exception:
        return True


def restore_test(cfg: Config, work: Path, manifest: dict) -> dict:
    """Restore this run's own dumps into a THROWAWAY database and compare.

    The server does not hold the private key (it is copied off and forgotten),
    so a later test could never decrypt a set. The proof is taken here instead,
    on the plaintext parts, before encryption: the dump goes into a fresh
    container, every table's row count must equal the manifest, and every hash
    chain must be exactly as the probe saw it. Temporal's dumps must load too.
    The container has no volume and is removed afterwards.
    """
    import secrets as _secrets
    import time
    if cfg.db_backend != "postgresql":
        return _restore_test_mysql(cfg, work, manifest)
    t0 = time.monotonic()
    name = f"velaris-restore-test-{os.getpid()}"
    pw = _secrets.token_hex(16)
    notes: list[str] = []
    try:
        r = run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_USER=velaris",
                 "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=velaris", "-p", "127.0.0.1::5432",
                 TEST_PG_IMAGE], env={**os.environ, "POSTGRES_PASSWORD": pw})
        cid = r.stdout.decode().strip()
        # initdb restarts the server once; wait for the real one. The init-time
        # server listens on the socket only, so ask over TCP: a socket probe
        # answered by it, then the restart, failed the test (R5b re-run 2026-10-30).
        for _ in range(60):
            time.sleep(1)
            ok = subprocess.run(["docker", "exec", cid, "psql", "-h", "127.0.0.1", "-U", "velaris", "-d", "velaris",
                                 "-Atc", "SELECT 1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if ok.returncode == 0 and b"1" in ok.stdout:
                break
        else:
            raise RuntimeError("throwaway database did not start")
        _recreate_pg_db(cid, "velaris", "velaris", work / manifest["database"]["file"])
        for db in (manifest.get("temporal") or {}).get("databases", []):
            _psql(cid, "velaris", "postgres", f'CREATE DATABASE "{db}";')
            with (work / f"{db}.pgdump").open("rb") as fh:
                rr = subprocess.run(["docker", "exec", "-i", cid, "pg_restore", "-U", "velaris", "-d", db,
                                     "--no-owner", "--role", "velaris", "--exit-on-error"],
                                    stdin=fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if rr.returncode != 0:
                raise RuntimeError(f"{db} did not load: " + rr.stderr.decode(errors="replace")[-200:])
        port = run(["docker", "port", cid, "5432/tcp"]).stdout.decode().strip().rsplit(":", 1)[-1]
        m = work / "manifest-for-test.json"
        m.write_text(json.dumps(manifest))
        env = _verifier_env(f"postgresql+asyncpg://velaris:{pw}@127.0.0.1:{port}/velaris")
        vr = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "backup" / "verify_restore.py"),
                             str(m), "--only", "row_counts,chains"], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = json.loads(vr.stdout.decode().strip().splitlines()[-1])
        for c in res["checks"]:
            notes.append(f"{c['name']}: {c['detail']}")
        passed = bool(res["ok"])
        if manifest.get("temporal", {}).get("included"):
            notes.append("temporal: both databases loaded")
        return {"result": "passed" if passed else "failed", "seconds": int(time.monotonic() - t0),
                "notes": " | ".join(notes)[:1000], "at": utcnow().isoformat()}
    except Exception as exc:
        return {"result": "failed", "seconds": int(time.monotonic() - t0),
                "notes": f"{type(exc).__name__}: {exc}"[:1000], "at": utcnow().isoformat()}
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _verifier_env(dsn: str) -> dict:
    """The verifier's environment for a THROWAWAY database. It runs from the
    install folder and reads its .env, whose pooler settings would send it to
    the LIVE database through PgBouncer instead (clean box 2026-10-23: every
    restore test since 6a compared the manifest with the running system) —
    emptied here, so it connects to `dsn` and nothing else."""
    return {**os.environ,
            "PYTHONPATH": os.pathsep.join([str(ROOT / "services" / "case-service"),
                                           str(ROOT / "services" / "_shared")]),
            "VELARIS_CASE_DATABASE_URL": dsn,
            "VELARIS_CASE_DATABASE_POOLER_URL": "", "VELARIS_CASE_DATABASE_POOLER_SOCKET": "false"}


def _verify_against(dsn: str, backend: str, manifest: dict, work: Path) -> dict:
    """verify_restore.py --only row_counts,chains against a throwaway database."""
    m = work / "manifest-for-test.json"
    m.write_text(json.dumps(manifest))
    env = {**_verifier_env(dsn), "VELARIS_CASE_DATABASE_BACKEND": backend}
    vr = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "backup" / "verify_restore.py"),
                         str(m), "--only", "row_counts,chains"], cwd=str(ROOT), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return json.loads(vr.stdout.decode().strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "checks": [{"name": "verifier", "ok": False,
                                         "detail": vr.stderr.decode(errors="replace")[-300:]}]}


def _restore_test_mysql(cfg: Config, work: Path, manifest: dict) -> dict:
    """The MySQL / MariaDB form of the throwaway restore test: a fresh server of
    the same family, the dump imported, row counts and chains compared."""
    import secrets as _secrets
    import time
    t0 = time.monotonic()
    maria = cfg.db_backend == "mariadb"
    image, client = (MARIADB_CLIENT_IMAGE, "mariadb") if maria else (MYSQL_CLIENT_IMAGE, "mysql")
    name = f"velaris-restore-test-{os.getpid()}"
    pw = _secrets.token_hex(16)
    env = {**os.environ, "MYSQL_ROOT_PASSWORD": pw, "MYSQL_PASSWORD": pw, "MYSQL_PWD": pw}
    try:
        cid = run(["docker", "run", "-d", "--rm", "--name", name, "-e", "MYSQL_ROOT_PASSWORD",
                   "-e", "MYSQL_DATABASE=velaris", "-e", "MYSQL_USER=velaris", "-e", "MYSQL_PASSWORD",
                   "-p", "127.0.0.1::3306", image], env=env).stdout.decode().strip()
        for _ in range(120):
            time.sleep(1)
            ok = subprocess.run(["docker", "exec", "-e", "MYSQL_PWD", cid, client, "-uvelaris", "-h", "127.0.0.1",
                                 "-N", "-e", "SELECT 1"], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if ok.returncode == 0 and b"1" in ok.stdout:
                break
        else:
            raise RuntimeError("throwaway MySQL/MariaDB did not start")
        with (work / manifest["database"]["file"]).open("rb") as fh:
            r = subprocess.run(["docker", "exec", "-i", "-e", "MYSQL_PWD", cid, client, "-uvelaris",
                                "-h", "127.0.0.1", "velaris"], stdin=fh, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError("import failed: " + r.stderr.decode(errors="replace")[-300:])
        port = run(["docker", "port", cid, "3306/tcp"]).stdout.decode().strip().rsplit(":", 1)[-1]
        res = _verify_against(f"mysql+aiomysql://velaris:{pw}@127.0.0.1:{port}/velaris",
                              cfg.db_backend, manifest, work)
        notes = [f"{c['name']}: {c['detail']}" for c in res["checks"]]
        return {"result": "passed" if res["ok"] else "failed", "seconds": int(time.monotonic() - t0),
                "notes": " | ".join(notes)[:1000], "at": utcnow().isoformat()}
    except Exception as exc:
        return {"result": "failed", "seconds": int(time.monotonic() - t0),
                "notes": f"{type(exc).__name__}: {exc}"[:1000], "at": utcnow().isoformat()}
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── encrypt, ship, prune ──────────────────────────────────────────────────

def encrypt(src: Path, dst: Path, recipient: str) -> None:
    """age encryption in-process (pyrage, streaming). Standard age format:
    `age -d -i key.txt set.tar.age > set.tar` works on any machine."""
    import pyrage
    from pyrage import x25519
    try:
        rcpt = x25519.Recipient.from_str(recipient)
    except Exception as exc:
        raise RuntimeError(f"VELARIS_BACKUP_AGE_RECIPIENT is not a valid age public key: {exc}") from exc
    pyrage.encrypt_file(str(src), str(dst), [rcpt])
    dst.chmod(0o600)


def _s3_client(cfg: Config):
    from minio import Minio  # only needed for an S3 destination
    s = cfg.s3
    endpoint = re.sub(r"^https?://", "", s["endpoint"]).rstrip("/")
    return Minio(endpoint, access_key=s["access_key"], secret_key=s["secret_key"],
                 secure=cfg.s3_secure, region=s["region"] or None)


def _s3_key(cfg: Config, name: str) -> str:
    prefix = cfg.s3["prefix"].strip("/")
    return f"{prefix}/{name}" if prefix else name


def ship(cfg: Config, files: list[Path]) -> dict:
    """Copy the set and its sidecar to every configured off-box destination."""
    result: dict[str, dict] = {}
    if cfg.nas_dir:
        try:
            cfg.nas_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                tmp = cfg.nas_dir / (f.name + ".partial")
                shutil.copyfile(f, tmp)
                tmp.chmod(0o600)
                if tmp.stat().st_size != f.stat().st_size:
                    raise RuntimeError(f"size mismatch copying {f.name}")
                tmp.replace(cfg.nas_dir / f.name)
            result["path"] = {"ok": True, "where": str(cfg.nas_dir)}
        except Exception as exc:
            result["path"] = {"ok": False, "where": str(cfg.nas_dir), "error": str(exc)[:300]}
    if cfg.s3["endpoint"] and cfg.s3["bucket"]:
        try:
            cli = _s3_client(cfg)
            for f in files:
                key = _s3_key(cfg, f.name)
                cli.fput_object(cfg.s3["bucket"], key, str(f))
                if cli.stat_object(cfg.s3["bucket"], key).size != f.stat().st_size:
                    raise RuntimeError(f"size mismatch uploading {f.name}")
            result["s3"] = {"ok": True, "where": f"s3://{cfg.s3['bucket']}/{_s3_key(cfg, '')}"}
        except Exception as exc:
            result["s3"] = {"ok": False, "where": f"s3://{cfg.s3['bucket']}", "error": str(exc)[:300]}
    return result


def _keep_set(names: list[str], keep: tuple[int, int, int],
              complete: dict[str, bool] | None = None) -> set[str]:
    """Grandfather-father-son over COMPLETE sets: the newest complete set of
    each of the last N days, weeks and months is kept.

    A partial set never takes a complete one's place — it only stays while it
    is newer than the newest complete set (it is then the latest thing there
    is). Unknown completeness (no sidecar) counts as complete, so an older
    layout is never pruned harder than before.
    """
    daily, weekly, monthly = keep
    complete = complete or {}
    dated = []
    for n in names:
        m = SET_RE.match(n)
        if m:
            dated.append((dt.datetime.strptime(m.group("stamp"), "%Y%m%dT%H%M%SZ"), n))
    dated.sort(reverse=True)
    full = [(d, n) for d, n in dated if complete.get(n, True)]
    newest_full = full[0][0] if full else None
    kept: set[str] = {n for d, n in dated
                      if not complete.get(n, True) and (newest_full is None or d > newest_full)}
    dated = full
    for width, key in ((daily, lambda d: d.date()),
                       (weekly, lambda d: d.isocalendar()[:2]),
                       (monthly, lambda d: (d.year, d.month))):
        seen = []
        for d, n in dated:
            k = key(d)
            if k not in seen:
                seen.append(k)
                if len(seen) > width:
                    break
                kept.add(n)
    return kept


def _sidecar(set_name: str) -> str:
    return set_name[:-len(".tar.age")] + ".json"


def _sidecar_complete(path: Path) -> bool:
    try:
        return bool(json.loads(path.read_text()).get("complete", True))
    except Exception:
        return True


def prune(cfg: Config) -> dict:
    """Retention per destination. Only files this engine names are ever touched."""
    out: dict[str, int] = {}

    def _dir(d: Path, label: str) -> None:
        if not d or not d.is_dir():
            return
        names = [p.name for p in d.iterdir() if SET_RE.match(p.name)]
        keep = _keep_set(names, cfg.keep, {n: _sidecar_complete(d / _sidecar(n)) for n in names})
        n = 0
        for name in names:
            if name not in keep:
                (d / name).unlink(missing_ok=True)
                (d / (name[:-len(".tar.age")] + ".json")).unlink(missing_ok=True)
                n += 1
        out[label] = n

    _dir(cfg.local_dir, "local")
    if cfg.nas_dir:
        _dir(cfg.nas_dir, "path")
    if cfg.s3["endpoint"] and cfg.s3["bucket"]:
        try:
            cli = _s3_client(cfg)
            prefix = _s3_key(cfg, "")
            objs = [o.object_name for o in cli.list_objects(cfg.s3["bucket"], prefix=prefix or None)]
            names = [o.rsplit("/", 1)[-1] for o in objs if SET_RE.match(o.rsplit("/", 1)[-1])]
            flags = {}
            for n in names:
                try:
                    resp = cli.get_object(cfg.s3["bucket"], _s3_key(cfg, _sidecar(n)))
                    flags[n] = bool(json.loads(resp.read()).get("complete", True))
                    resp.close()
                except Exception:
                    flags[n] = True
            keep = _keep_set(names, cfg.keep, flags)
            n = 0
            for name in names:
                if name not in keep:
                    cli.remove_object(cfg.s3["bucket"], _s3_key(cfg, name))
                    cli.remove_object(cfg.s3["bucket"], _s3_key(cfg, name[:-len(".tar.age")] + ".json"))
                    n += 1
            out["s3"] = n
        except Exception as exc:
            warn(f"! S3 retention skipped: {exc}")
    return out


def record(location: str, *, scope: str, status: str, encrypted: bool, error: str = "",
           test: dict | None = None) -> None:
    """HxDR bookkeeping. Never changes the backup's own outcome."""
    py = ROOT / ".venv" / "bin" / "python"
    if not py.is_file():
        warn("! .venv not found — backup not recorded in /dr/posture")
        return
    cmd = [str(py), "-m", "case_service.compliance.dr_cli", "--location", location,
           "--scope", scope, "--status", status, "--recorded-by", "backup-velaris.sh"]
    if encrypted:
        cmd.append("--encrypted")
    if error:
        cmd += ["--error", error[:500]]
    if test and test.get("result") in ("passed", "failed"):
        cmd += ["--restore-test", test["result"], "--restore-test-seconds", str(test.get("seconds", 0)),
                "--restore-test-notes", test.get("notes", "")[:900]]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "services" / "case-service")}
    r = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        warn("! backup NOT recorded in HxDR — /dr/posture will under-report")


# ── status the platform reads (posture, Studio, the watcher) ──────────────

STATUS_FILE = "status.json"


def write_status(cfg: Config, **fields) -> None:
    """`.backups/status.json`: what the last run did — never a secret. The
    platform (HxDR posture, Studio, the backup watcher) reads it; it cannot see
    systemd or this directory any other way."""
    try:
        prev = json.loads((cfg.local_dir / STATUS_FILE).read_text())
    except Exception:
        prev = {}
    st = {**prev, **fields, "updated_at": utcnow().isoformat(),
          "key_configured": bool(cfg.recipient),
          "offbox_configured": bool(cfg.nas_dir or (cfg.s3["endpoint"] and cfg.s3["bucket"])),
          "timer_installed": Path("/etc/systemd/system/velaris-backup.timer").is_file()}
    try:
        cfg.local_dir.mkdir(parents=True, exist_ok=True)
        tmp = cfg.local_dir / (STATUS_FILE + ".tmp")
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True))
        tmp.chmod(0o644)   # the platform may run as another user; nothing secret in it
        tmp.replace(cfg.local_dir / STATUS_FILE)
    except Exception as exc:
        warn(f"! could not write {STATUS_FILE}: {exc}")


# ── commands ──────────────────────────────────────────────────────────────

def _clear_stale_work(cfg: Config) -> None:
    """A run killed mid-way leaves its working directory behind — decrypted
    dumps and a plain .env. Owner-only, but it must not linger: any working
    directory older than an hour is removed by the next run."""
    import time
    if not cfg.local_dir.is_dir():
        return
    for d in cfg.local_dir.iterdir():
        if d.is_dir() and d.name.startswith((".velaris-backup-", ".velaris-restore-")) \
                and time.time() - d.stat().st_mtime > 3600:
            shutil.rmtree(d, ignore_errors=True)
            warn(f"  ! removed a working directory left by an interrupted run: {d.name}")


def _free_space_ok(cfg: Config) -> tuple[bool, str]:
    """Checked BEFORE the dump: at least twice the newest set, and 1 GiB."""
    cfg.local_dir.mkdir(parents=True, exist_ok=True)
    # Owner-only: the sets are encrypted, but their names (host, times,
    # versions) and the sidecars are nobody else's business either.
    try:
        cfg.local_dir.chmod(0o700)
    except OSError:
        pass
    last = max((p.stat().st_size for p in cfg.local_dir.iterdir() if SET_RE.match(p.name)), default=0)
    need = max(2 * last, 1 << 30)
    free = shutil.disk_usage(cfg.local_dir).free
    return free >= need, f"free {free >> 20} MiB, need {need >> 20} MiB"


def cmd_backup(args) -> int:
    cfg = Config()
    if args.dir:
        cfg.local_dir = Path(args.dir)
    if not cfg.recipient:
        return legacy_dump(cfg, args)

    _clear_stale_work(cfg)
    ok, why = _free_space_ok(cfg)
    if not ok:
        warn(f"✗ Not enough disk for a backup in {cfg.local_dir} ({why}) — nothing taken")
        record(str(cfg.local_dir), scope="full", status="failed", encrypted=True, error=f"disk: {why}")
        write_status(cfg, last_run_at=utcnow().isoformat(), last_outcome="failed", last_error=f"disk: {why}")
        return 1

    started = utcnow()
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    base = f"velaris-{cfg.host}-{stamp}-v{cfg.version}"
    final = cfg.local_dir / f"{base}.tar.age"
    sidecar = cfg.local_dir / f"{base}.json"
    manifest: dict = {
        "format": 1, "kind": "velaris-backup-set", "host": cfg.host, "version": cfg.version,
        "created_at": started.isoformat(), "database": {}, "documents": {}, "openbao": {},
        "parts_missing": [], "files": {},
    }
    work = Path(tempfile.mkdtemp(prefix=".velaris-backup-", dir=str(cfg.local_dir)))
    work.chmod(0o700)
    try:
        say(f"▶ Backup set {final.name}")
        files: list[Path] = []
        try:
            files.append(dump_postgres(cfg, work, manifest) if cfg.db_backend == "postgresql"
                         else dump_mysql(cfg, work, manifest))
            say(f"  ✓ database ({cfg.db_backend}, {len(manifest['database'].get('row_counts', {}))} tables)")
        except Exception as exc:
            # Without the database there is no backup worth the name.
            raise RuntimeError(f"database: {exc}") from exc
        manifest["probe"] = _probe(manifest["database"].get("dump_started_at"))
        for label, fn in (("documents", capture_documents), ("temporal", capture_temporal),
                          ("settings", capture_settings), ("openbao", capture_openbao)):
            try:
                got = fn(cfg, work, manifest)
                got = [got] if isinstance(got, Path) else (got or [])
                files += got
                if label == "openbao" and cfg.openbao_enabled and not manifest["openbao"].get("included"):
                    warn(f"  ✗ openbao: {manifest['openbao'].get('note', 'not captured')}")
                elif label == "documents" and manifest["documents"].get("source") == "external-s3":
                    say("  – documents: in the operator's own S3 service (not in this set)")
                elif label == "temporal" and manifest["temporal"].get("external"):
                    say("  – temporal: held externally — back it up there (not in this set)")
                else:
                    say(f"  ✓ {label}")
            except Exception as exc:
                manifest["parts_missing"].append({"part": label, "error": str(exc)[:300]})
                warn(f"  ✗ {label}: {exc}")
        if not manifest["documents"].get("included", False) and manifest["documents"].get("source") != "external-s3":
            manifest["parts_missing"].append({"part": "documents", "error": "not captured"})
        if not manifest.get("temporal", {}).get("included", False) \
                and not manifest.get("temporal", {}).get("external"):
            manifest["parts_missing"].append({"part": "temporal", "error": manifest.get("temporal", {}).get("note", "")})
        if not manifest["openbao"].get("included", False) and cfg.openbao_enabled:
            manifest["parts_missing"].append({"part": "openbao", "error": manifest["openbao"].get("note", "")})

        # One entry per missing part (a part that raised is also seen as absent).
        _seen: set[str] = set()
        manifest["parts_missing"] = [p for p in manifest["parts_missing"]
                                     if not (p["part"] in _seen or _seen.add(p["part"]))]
        for f in files:
            manifest["files"][str(f.relative_to(work))] = {"sha256": sha256(f), "bytes": f.stat().st_size}
        manifest["finished_at"] = utcnow().isoformat()
        complete = not manifest["parts_missing"]
        manifest["complete"] = complete
        force = True if getattr(args, "test_restore", False) else (False if getattr(args, "no_test_restore", False) else None)
        test = None
        if _restore_test_due(cfg, force):
            say("  ▶ proving it restores (throwaway database)...")
            test = restore_test(cfg, work, manifest)
            manifest["restore_test"] = test
            if test.get("result") == "passed":
                say(f"  ✓ restore test passed in {test['seconds']} s")
                st = cfg.local_dir / RESTORE_TEST_STATE
                st.write_text(json.dumps({"last_passed_at": test["at"], "set": final.name}))
                st.chmod(0o600)
            elif test.get("result") == "failed":
                warn(f"  ✗ restore test FAILED — {test['notes'][:300]}")
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

        plain = work / "set.tar"
        with tarfile.open(plain, "w") as t:
            t.add(str(work / "manifest.json"), arcname="manifest.json")
            for f in files:
                t.add(str(f), arcname=str(f.relative_to(work)))
        encrypt(plain, final, cfg.recipient)
        set_sha = sha256(final)

        public = {
            "set": final.name, "sha256": set_sha, "bytes": final.stat().st_size,
            "created_at": manifest["created_at"], "finished_at": manifest["finished_at"],
            "version": cfg.version, "host": cfg.host, "encrypted": True,
            "recipient": cfg.recipient, "complete": complete,
            "parts": {"database": cfg.db_backend,
                      "documents": manifest["documents"].get("source"),
                      "documents_included": manifest["documents"].get("included", False),
                      "temporal": manifest.get("temporal", {}).get("included", False),
                      "settings": manifest["settings"].get("env_included", False),
                      "openbao": manifest["openbao"].get("included", False)},
            "parts_missing": [p["part"] for p in manifest["parts_missing"]],
            "migrations": manifest["database"].get("migrations"),
        }
        sidecar.write_text(json.dumps(public, indent=2, sort_keys=True))
        sidecar.chmod(0o600)
        say(f"  ✓ encrypted set {final.stat().st_size >> 20} MiB  sha256 {set_sha[:16]}…")

        shipped = ship(cfg, [final, sidecar])
        # The deletion ledger rides along: a restore needs it most when this box is gone.
        dres = push_deletions(cfg)
        for k, v in dres.items():
            if isinstance(v, dict) and v.get("ok") is False:
                warn(f"  ✗ deletion records not copied to {k}: {v.get('error')}")
        for dest, res in shipped.items():
            (say if res["ok"] else warn)(f"  {'✓' if res['ok'] else '✗'} copied to {dest}: {res['where']}"
                                         + ("" if res["ok"] else f" — {res.get('error')}"))
        if not shipped:
            warn("  ! no off-box destination set — this backup does not survive losing this server")
        pruned = {} if getattr(args, "no_prune", False) else prune(cfg)
        if any(pruned.values()):
            say("  pruned " + ", ".join(f"{k}: {v}" for k, v in pruned.items() if v))

        offbox_ok = any(r["ok"] for r in shipped.values())
        scope = "full" if complete else "partial"
        record(str(final), scope=scope, status="succeeded", encrypted=True, test=test,
               error="" if (complete and (offbox_ok or not shipped)) else
               "; ".join([p["part"] + " missing" for p in manifest["parts_missing"]]
                         + [f"{k} copy failed" for k, r in shipped.items() if not r["ok"]]))
        outcome = ("test_failed" if test and test.get("result") == "failed" else
                   "partial" if not complete else
                   "no_offbox_copy" if (shipped and not offbox_ok) else "ok")
        status = {"last_run_at": manifest["finished_at"], "last_outcome": outcome,
                  "last_set": final.name, "last_set_complete": complete, "last_encrypted": True,
                  "last_offbox": {k: r["ok"] for k, r in shipped.items()},
                  "parts_missing": [p["part"] for p in manifest["parts_missing"]]}
        if outcome != "test_failed" and complete:
            status["last_good_at"] = manifest["finished_at"]
        if test:
            status["last_restore_test"] = {"result": test.get("result"), "at": test.get("at"),
                                           "seconds": test.get("seconds")}
            if test.get("result") == "passed":
                status["last_proven_restore_at"] = test.get("at")
        write_status(cfg, **status)
        if test and test.get("result") == "failed":
            warn("! this set did NOT pass its restore test")
            return 5
        if not complete:
            warn("! PARTIAL backup — " + ", ".join(p["part"] for p in manifest["parts_missing"]) + " missing")
            return 3
        if shipped and not offbox_ok:
            warn("! backup taken but NO off-box copy succeeded")
            return 4
        say("✓ Done.")
        return 0
    except Exception as exc:
        warn(f"✗ Backup FAILED — {exc}")
        final.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        record(str(final), scope="full", status="failed", encrypted=True, error=str(exc))
        write_status(cfg, last_run_at=utcnow().isoformat(), last_outcome="failed",
                     last_error=str(exc)[:300])
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def legacy_dump(cfg: Config, args) -> int:
    """No recipient: exactly what backups were before (database only, local,
    plain SQL), so an install without a key is never worse off — but nothing
    secret is ever written unencrypted."""
    warn("! No backup key (VELARIS_BACKUP_AGE_RECIPIENT) — database-only, UNENCRYPTED, local dump. "
         "Run: .venv/bin/python scripts/backup/velaris_backup.py ensure-key")
    cfg.local_dir.mkdir(parents=True, exist_ok=True)
    try:
        cfg.local_dir.chmod(0o700)
    except OSError:
        pass
    out = cfg.local_dir / f"velaris-{utcnow().strftime('%Y%m%d-%H%M%S')}.sql"
    try:
        if cfg.db_backend == "postgresql":
            cid = None if "database" in cfg.external else container_of("velaris-db")
            if "database" not in cfg.external and not cid:
                raise RuntimeError("the velaris-db container is not running")
            argv, env = pg_cmd(cfg, "pg_dump", cid=cid)
            with out.open("wb") as f:
                r = subprocess.run(argv, env=env, stdout=f, stderr=subprocess.PIPE)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
        else:
            work = Path(tempfile.mkdtemp(dir=str(cfg.local_dir)))
            try:
                shutil.move(str(dump_mysql(cfg, work, {"database": {}})), out)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        out.chmod(0o600)
    except Exception as exc:
        out.unlink(missing_ok=True)
        warn(f"✗ Backup FAILED — {exc}")
        record(str(out), scope="database", status="failed", encrypted=False, error=str(exc))
        write_status(cfg, last_run_at=utcnow().isoformat(), last_outcome="failed", last_error=str(exc)[:300])
        return 1
    say(f"  ✓ {out} ({out.stat().st_size >> 10} KiB)")
    record(str(out), scope="database", status="succeeded", encrypted=False)
    write_status(cfg, last_run_at=utcnow().isoformat(), last_outcome="unencrypted_database_only",
                 last_set=out.name, last_set_complete=False, last_encrypted=False)
    if args.keep:
        cutoff = utcnow() - dt.timedelta(days=int(args.keep))
        for p in cfg.local_dir.glob("velaris-*.sql"):
            if dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc) < cutoff:
                p.unlink(missing_ok=True)
    return 0


def _push_openbao(key: str, value: str) -> None:
    if not (ROOT / "deploy" / "openbao" / "enabled").is_file():
        return
    r = subprocess.run([str(ROOT / "scripts" / "secrets-push.sh"), f"{key}={value}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        warn("! could not store it in OpenBao — run: ./scripts/secrets-push.sh --sync-env")


def cmd_ensure_key(args) -> int:
    """Give this install a backup key pair if it has none.

    The PUBLIC key goes to .env (and OpenBao). The PRIVATE key goes only into
    the install report, mode 0600, between markers, with instructions to copy
    it off this server and then run `forget-private-key`. It is never logged,
    never in .env, never in OpenBao, never inside a backup set.
    """
    cfg = Config()
    if cfg.recipient:
        say("  ✓ backup key already set (VELARIS_BACKUP_AGE_RECIPIENT)")
        return 0
    if args.recipient:
        pub = args.recipient.strip()
        try:
            from pyrage import x25519
            x25519.Recipient.from_str(pub)
        except Exception:
            warn("✗ --recipient must be an age public key (age1…)")
            return 2
        priv = None
    else:
        from pyrage import x25519
        ident = x25519.Identity.generate()
        pub = str(ident.to_public())
        # The same text `age-keygen` writes, so the operator can use it as-is:
        #   age -d -i backup-key.txt <set>.tar.age > set.tar
        priv = (f"# created: {utcnow().isoformat()}\n# public key: {pub}\n{ident}\n")

    env = ROOT / ".env"
    lines = env.read_text().splitlines() if env.is_file() else []
    lines = [l for l in lines if not l.startswith("VELARIS_BACKUP_AGE_RECIPIENT=")]
    lines.append(f"VELARIS_BACKUP_AGE_RECIPIENT={pub}")
    env.write_text("\n".join(lines) + "\n")
    if not args.no_push:
        _push_openbao("VELARIS_BACKUP_AGE_RECIPIENT", pub)
    say("  ✓ backup public key set (VELARIS_BACKUP_AGE_RECIPIENT)")

    if priv:
        write_report_key(priv, pub)
        say(f"  ✓ backup PRIVATE key written to {REPORT.name} — copy it off this server, then run:")
        say("      ./backup-velaris.sh --forget-private-key")
    return 0


def write_report_key(priv: str, pub: str) -> None:
    text = REPORT.read_text() if REPORT.is_file() else "# Velaris install report\n"
    text = re.sub(re.escape(KEY_BEGIN) + r".*?" + re.escape(KEY_END) + r"\n?", "", text, flags=re.S)
    block = f"""{KEY_BEGIN}
## ⚠ Backup private key — COPY THIS OFF THIS SERVER NOW

Every backup of this install is encrypted to the public key below. **Without the
private key no backup can be restored** — not by you, not by Velaris support.

1. Copy everything between the two lines below into a password manager or onto
   offline media, **not** onto this server. Keep two copies in different places.
2. Then remove it from this server:  `./backup-velaris.sh --forget-private-key`

While it stays here, anyone who takes this server can also read its backups.
Studio and the recovery posture warn until it is removed.

Public key: `{pub}`
Generated: {utcnow().isoformat()}

```
{priv.strip()}
```
{KEY_END}
"""
    REPORT.write_text(text.rstrip() + "\n\n" + block)
    REPORT.chmod(0o600)


def cmd_forget_private_key(args) -> int:
    if not REPORT.is_file() or KEY_BEGIN not in REPORT.read_text():
        say("  ✓ no backup private key in the install report")
        return 0
    text = REPORT.read_text()
    text = re.sub(re.escape(KEY_BEGIN) + r".*?" + re.escape(KEY_END),
                  f"## Backup private key\n\nRemoved from this server on {utcnow().isoformat()}. "
                  "It is wherever you copied it to.", text, flags=re.S)
    tmp = REPORT.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(REPORT)
    say("  ✓ backup private key removed from the install report")
    return 0


# ── restore ───────────────────────────────────────────────────────────────

APP_PORT_KEYS = ("VELARIS_CASE_PORT", "VELARIS_ANALYTICS_PORT", "VELARIS_HXGRAPH_PORT",
                 "VELARIS_HXMIGRATE_PORT", "VELARIS_COMMERCE_PORT", "VELARIS_PORTAL_PORT",
                 "VELARIS_USER_PORT", "VELARIS_WORKER_PORT", "VELARIS_ENGINE_PORT")
APP_PORT_DEFAULTS = (8201, 8202, 8203, 8204, 8207, 8205, 8206, 8208, 8100)
LAST_MANIFEST = "last-restore-manifest.json"
SYSTEMD_UNITS = ("velaris-engine", "velaris-case-service", "velaris-analytics-service",
                 "velaris-hxgraph-service", "velaris-hxmigrate-service",
                 "velaris-commerce-service", "velaris-portal-service",
                 "velaris-user-service", "velaris-worker-service")


def _installed_units(studio: bool = False) -> str:
    """The units this box has, for the stop/start command we print. Until its own
    unit is installed, commerce-, portal- and user-service run inside case-service's (R5e 7, 8, 10),
    and naming a unit systemd does not know makes the command fail."""
    return " ".join(u for u in SYSTEMD_UNITS + (("velaris-studio",) if studio else ()) if subprocess.run(
        ["systemctl", "cat", u], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0)


def _active_units() -> list[str]:
    """The platform's systemd units that are active (or activating) now."""
    if not shutil.which("systemctl"):
        return []                     # no systemd on this box: nothing it supervises
    out = []
    for u in SYSTEMD_UNITS + ("velaris-studio",):
        r = subprocess.run(["systemctl", "is-active", u], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True)
        if r.stdout.strip() in ("active", "activating", "reloading"):
            out.append(u)
    return out


def _systemd_managed() -> bool:
    """Installed systemd units supervise the platform (they restart a killed
    process at once, so only `systemctl stop` stops it)."""
    return any(subprocess.run(["systemctl", "cat", u], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0 for u in SYSTEMD_UNITS)


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _load_identities(path: str):
    from pyrage import x25519
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    ids = [x25519.Identity.from_str(l.strip()) for l in text.splitlines()
           if l.strip().startswith("AGE-SECRET-KEY-")]
    if not ids:
        raise RuntimeError("no AGE-SECRET-KEY- line in the key file")
    return ids


# ── the deletion ledger (Nov_TODO "User deletion v2" part b) ────────────────
#
# Purged accounts' signed records live in <install>/.deletions (written by the
# platform, velaris_shared.identity.ledger). A restore of a set taken before a
# purge would bring the account back; the records undo that, so they must
# survive the box: every backup run — and `--push-deletions`, which a path unit
# runs the moment one is written — copies them to every off-box destination
# (`<path>/deletions/`, `s3://<bucket>/<prefix>/deletions/`). Append-only: a
# record already there is never rewritten, nothing is ever deleted.

def deletions_dir(cfg: "Config") -> Path:
    d = (cfg.env.get("VELARIS_CASE_DELETION_LEDGER_DIR") or "").strip()
    return Path(d) if d else ROOT / ".deletions"


def push_deletions(cfg: "Config") -> dict:
    src = deletions_dir(cfg)
    files = sorted(src.glob("*.json")) if src.is_dir() else []
    result: dict[str, dict] = {"records": {"count": len(files)}}
    if not files:
        return result
    if cfg.nas_dir:
        try:
            dst = cfg.nas_dir / "deletions"
            dst.mkdir(parents=True, exist_ok=True)
            n = 0
            for f in files:
                if not (dst / f.name).exists():
                    tmp = dst / (f.name + ".partial")
                    shutil.copyfile(f, tmp)
                    tmp.chmod(0o600)
                    tmp.replace(dst / f.name)
                    n += 1
            result["path"] = {"ok": True, "new": n, "where": str(dst)}
        except Exception as exc:
            result["path"] = {"ok": False, "error": str(exc)[:300]}
    if cfg.s3["endpoint"] and cfg.s3["bucket"]:
        try:
            cli = _s3_client(cfg)
            have = {o.object_name.rsplit("/", 1)[-1] for o in cli.list_objects(
                cfg.s3["bucket"], prefix=_s3_key(cfg, "deletions/"), recursive=True)}
            n = 0
            for f in files:
                if f.name not in have:
                    cli.fput_object(cfg.s3["bucket"], _s3_key(cfg, "deletions/" + f.name), str(f))
                    n += 1
            result["s3"] = {"ok": True, "new": n}
        except Exception as exc:
            result["s3"] = {"ok": False, "error": str(exc)[:300]}
    return result


def collect_deletions(cfg: "Config") -> tuple[int, list[str]]:
    """Every record reachable — the off-box copies (NAS, S3) — merged into
    the local ledger folder. (n merged, problems)."""
    dst = deletions_dir(cfg)
    dst.mkdir(mode=0o700, parents=True, exist_ok=True)
    merged, problems = 0, []
    if cfg.nas_dir and (cfg.nas_dir / "deletions").is_dir():
        for f in sorted((cfg.nas_dir / "deletions").glob("*.json")):
            if not (dst / f.name).exists():
                shutil.copyfile(f, dst / f.name)
                (dst / f.name).chmod(0o600)
                merged += 1
    if cfg.s3["endpoint"] and cfg.s3["bucket"]:
        try:
            cli = _s3_client(cfg)
            for o in cli.list_objects(cfg.s3["bucket"], prefix=_s3_key(cfg, "deletions/"), recursive=True):
                name = o.object_name.rsplit("/", 1)[-1]
                if name.endswith(".json") and not (dst / name).exists():
                    cli.fget_object(cfg.s3["bucket"], o.object_name, str(dst / name))
                    (dst / name).chmod(0o600)
                    merged += 1
        except Exception as exc:
            problems.append(f"s3: {str(exc)[:200]}")
    return merged, problems


def replay_deletions(cfg: "Config") -> dict:
    """Purge every account a verified record names that the restored database
    still holds — through the platform's own code, directly on the database
    (the pooler is not started during a restore)."""
    py = ROOT / ".venv" / "bin" / "python"
    env = {**os.environ, "VELARIS_CASE_DATABASE_POOLER_URL": "",
           "VELARIS_CASE_DATABASE_POOLER_SOCKET": "false",
           "PYTHONPATH": str(ROOT / "services" / "_shared")}
    r = subprocess.run([str(py), "-m", "velaris_shared.identity.ledger_cli", "replay",
                        "--dir", str(deletions_dir(cfg))],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}
    out = json.loads(lines[-1])
    out["ok"] = r.returncode == 0
    return out


def cmd_push_deletions(args) -> int:
    cfg = Config()
    res = push_deletions(cfg)
    bad = [k for k, v in res.items() if isinstance(v, dict) and v.get("ok") is False]
    for k in bad:
        warn(f"✗ deletion records not copied to {k}: {res[k].get('error')}")
    if not QUIET:
        say(f"✓ deletion records: {res['records']['count']} here; "
            + ", ".join(f"{k} +{v.get('new', 0)}" for k, v in res.items() if k != "records" and v.get("ok"))
            if res["records"]["count"] else "✓ no deletion records yet")
    return 4 if bad else 0


def _fetch_set(cfg: Config, src: str, work: Path) -> Path:
    if src.startswith("s3://"):
        bucket, _, key = src[5:].partition("/")
        dst = work / key.rsplit("/", 1)[-1]
        _s3_client(cfg).fget_object(bucket, key, str(dst))
        return dst
    p = Path(src)
    if not p.is_file():
        raise RuntimeError(f"no such set: {src}")
    return p


def _safe_extract(tar_path: Path, dest: Path, strip: str = "") -> None:
    """Extract with the stdlib's 'data' filter (no absolute paths, no `..`,
    no devices, no links out of dest)."""
    with tarfile.open(tar_path) as t:
        members = []
        for m in t.getmembers():
            if strip:
                if not (m.name == strip.rstrip("/") or m.name.startswith(strip)):
                    continue
                m.name = m.name[len(strip):] if m.name.startswith(strip) else ""
                if not m.name:
                    continue
            members.append(m)
        t.extractall(dest, members=members, filter="data")


def _psql(cid: str, user: str, db: str, sql: str) -> str:
    return run(["docker", "exec", "-i", cid, "psql", "-U", user, "-d", db, "-At", "-v", "ON_ERROR_STOP=1"],
               input_bytes=sql.encode()).stdout.decode().strip()


def _wait_pg(cid: str, user: str, tries: int = 60) -> None:
    import time
    for _ in range(tries):
        if subprocess.run(["docker", "exec", cid, "pg_isready", "-U", user, "-q"]).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("database did not become ready")


def _recreate_pg_db(cid: str, owner: str, db: str, dump: Path) -> None:
    lit = db.replace("'", "''")
    _psql(cid, owner, "postgres",
          f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{lit}' "
          "AND pid <> pg_backend_pid();")
    _psql(cid, owner, "postgres", f'DROP DATABASE IF EXISTS "{db}";')
    _psql(cid, owner, "postgres", f'CREATE DATABASE "{db}" OWNER "{owner}";')
    with dump.open("rb") as fh:
        r = subprocess.run(["docker", "exec", "-i", cid, "pg_restore", "-U", owner, "-d", db,
                            "--exit-on-error", "--no-owner", "--role", owner],
                           stdin=fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"pg_restore {db}: " + r.stderr.decode(errors="replace")[-400:])


def _bao_health() -> int:
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen("http://127.0.0.1:8350/v1/sys/health", timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _bao(cmd: str, token: str | None = None, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Run a bao command in the server container. A token goes in on stdin
    (first line), never on a command line."""
    script = ("read -r BAO_TOKEN; export BAO_TOKEN; " if token else "") + cmd
    data = ((token + "\n").encode() if token else b"") + (stdin or b"")
    return subprocess.run(["docker", "exec", "-i", "-e", "BAO_ADDR=http://127.0.0.1:8200", "velaris-openbao",
                           "sh", "-c", script], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _aside_dir(stamp: str) -> Path:
    """Where restore keeps what it replaced: inside the owner-only backup
    directory, never next to the originals (a copy of .env or of the vault's
    init file beside the original is one `git add .` or one mirror sync away
    from leaving the box)."""
    d = Config().local_dir / f"before-restore-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _restore_openbao(x: Path, stamp: str) -> str:
    """Replace this box's vault with the set's, then open it with the set's key."""
    import time
    obao = ROOT / "deploy" / "openbao"
    subprocess.run([str(ROOT / "scripts" / "openbao-storage-migrate.sh")], check=False)
    run(compose("up", "-d", "openbao"))
    for _ in range(30):
        code = _bao_health()
        if code:
            break
        time.sleep(1)
    cur_init = obao / ".bao-init.json"
    if code == 501:      # never initialised: a throwaway init only to be allowed to restore
        r = _bao("bao operator init -key-shares=1 -key-threshold=1 -format=json")
        if r.returncode != 0:
            raise RuntimeError("could not initialise the empty vault: " + r.stderr.decode()[-200:])
        tmp = json.loads(r.stdout)
        _bao(f"bao operator unseal {tmp['unseal_keys_b64'][0]} >/dev/null")
        token = tmp["root_token"]
    else:
        if not cur_init.is_file():
            raise RuntimeError("this vault is initialised but its .bao-init.json is missing")
        cur = json.loads(cur_init.read_text())
        if code == 503:          # sealed: open it with this box's own key first
            subprocess.run([str(ROOT / "scripts" / "secrets-unseal.sh")], stdout=subprocess.DEVNULL, check=False)
        token = cur["root_token"]
    snap = x / "openbao.snap"
    r = _bao("cat > /tmp/r.snap && bao operator raft snapshot restore -force /tmp/r.snap; rc=$?; rm -f /tmp/r.snap; exit $rc",
             token=token, stdin=snap.read_bytes())
    if r.returncode != 0:
        raise RuntimeError("vault snapshot restore failed: " + r.stderr.decode(errors="replace")[-300:])
    # The set's init file and AppRole files replace this box's (kept aside).
    for name, dst in ((".bao-init.json", cur_init), ("agent_role_id", obao / "agent" / "role_id"),
                      ("agent_secret_id", obao / "agent" / "secret_id")):
        src = x / "openbao" / name
        if src.is_file():
            if dst.is_file():
                kept = _aside_dir(stamp) / ("openbao_" + name)
                shutil.copy2(dst, kept)
                kept.chmod(0o600)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            dst.chmod(0o600)
    # After a snapshot restore the server reloads its storage for a few
    # seconds; unsealing too early fails although nothing is wrong.
    for _ in range(30):
        time.sleep(1)
        code = _bao_health()
        if code == 200:
            return "restored; open with the set's own keys"
        if code in (503, 472):
            r = subprocess.run([str(ROOT / "scripts" / "secrets-unseal.sh")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode == 0 and _bao_health() == 200:
                return "restored and opened with the set's own unseal key"
    raise RuntimeError(f"the restored vault did not open with the set's unseal key (health {_bao_health()})")


def _restore_documents(cfg: Config, x: Path, manifest: dict, stamp: str, replace: bool) -> str:
    doc = manifest.get("documents", {})
    if not doc.get("included"):
        return "not in the set (" + doc.get("source", "unknown") + ")"
    tarp = x / "documents.tar"
    if doc.get("source") == "local":
        target = cfg.storage_local_path
        if target.exists() and any(target.iterdir()):
            aside = target.with_name(target.name + f".before-restore-{stamp}")
            target.rename(aside)
        target.mkdir(parents=True, exist_ok=True)
        _safe_extract(tarp, target, strip="documents/")
        return f"local store restored to {target}"
    if doc.get("source") == "bundled-minio":
        vol = f"{compose_project()}_velaris-minio-data"
        run(compose("stop", "minio"), check=False)
        wipe = "find /v -mindepth 1 -delete; " if replace else ""
        with tarp.open("rb") as fh:
            r = subprocess.run(["docker", "run", "--rm", "-i", "-v", f"{vol}:/v", VOLUME_READER_IMAGE,
                                "sh", "-c", f"{wipe}tar -C /v -xf -"], stdin=fh,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError("restoring the MinIO volume failed: " + r.stderr.decode()[-300:])
        run(compose("up", "-d", "minio"), check=False)
        return f"bundled MinIO volume {vol} restored"
    return "nothing to do"


def cmd_restore(args) -> int:
    import time
    t0 = time.monotonic()
    cfg = Config()
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    cfg.local_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_work(cfg)
    work = Path(tempfile.mkdtemp(prefix=".velaris-restore-", dir=str(cfg.local_dir)))
    work.chmod(0o700)
    steps: list[str] = []
    try:
        # 1. Open the set and prove it is intact before touching anything.
        ids = _load_identities(args.key)
        src = _fetch_set(cfg, args.set, work)
        say(f"▶ Restoring from {src.name}")
        import pyrage
        plain = work / "set.tar"
        try:
            pyrage.decrypt_file(str(src), str(plain), ids)
        except Exception as exc:
            raise RuntimeError(f"cannot decrypt the set with this key ({type(exc).__name__})") from exc
        x = work / "x"
        x.mkdir()
        _safe_extract(plain, x)
        plain.unlink()
        manifest = json.loads((x / "manifest.json").read_text())
        if manifest.get("kind") != "velaris-backup-set":
            raise RuntimeError("not a Velaris backup set")
        bad = [f for f, v in manifest["files"].items() if sha256(x / f) != v["sha256"]]
        if bad:
            raise RuntimeError(f"checksum mismatch: {bad}")
        say(f"  ✓ decrypted; {len(manifest['files'])} files match their checksums")
        if not manifest.get("complete", False):
            warn("  ! this set is PARTIAL — missing: " + ", ".join(p["part"] for p in manifest["parts_missing"]))

        # 2. Refusals.
        if _ver(manifest["version"]) > _ver(cfg.version):
            raise RuntimeError(f"the set is from Velaris {manifest['version']}, newer than this code "
                               f"({cfg.version}) — update the code first")
        backend = manifest["database"].get("backend")
        if (backend == "postgresql") != (cfg.db_backend == "postgresql"):
            raise RuntimeError(f"the set's database is {backend}; this install is {cfg.db_backend}")
        last = (manifest["database"].get("migrations") or {}).get("last") or ""
        mdir = ROOT / "migrations" / ("postgresql" if backend == "postgresql" else "mysql")
        newest = max((p.name for p in mdir.glob("*.sql")), default="")
        if last and newest and last > newest:
            raise RuntimeError(f"the set has migration {last}; this code knows only up to {newest}")
        if "database" in cfg.external:
            # Not ours to overwrite from here: restore database.pgdump / .sql on
            # that server with its own tools (docs/recovery-runbook.md).
            raise RuntimeError("this install's database is on another server (VELARIS_EXTERNAL_SERVICES"
                               "=database) — restore it there; see docs/recovery-runbook.md")
        busy = [p for k, d in zip(APP_PORT_KEYS, APP_PORT_DEFAULTS)
                if _listening(p := int(cfg.env.get(k) or d))]
        # A unit systemd still runs is running, even in the seconds after its
        # process was killed and before systemd restarts it — the ports alone
        # missed that, and the restarted services wrote into the restore
        # (clean box 2026-10-23: job_leases, "could not create unique index").
        busy += [u for u in _active_units()]
        if busy:
            how = ("sudo systemctl stop " + _installed_units(studio=True)) if _systemd_managed() \
                else "./stop-velaris.sh"
            raise RuntimeError(f"the platform is running (ports {busy}) — stop it first: {how}")

        # 3. Bring up only what a restore needs.
        needed = ["velaris-db", "temporal-db"] if backend == "postgresql" else ["temporal-db"]
        if "temporal" in cfg.external:
            needed.remove("temporal-db")
        if needed:
            run(compose("up", "-d", *needed))
        if backend == "postgresql":
            cid = container_of("velaris-db")
            _wait_pg(cid, "velaris")
            existing = _psql(cid, "velaris", "velaris",
                             "SELECT CASE WHEN to_regclass('public.schema_migrations') IS NULL THEN 0 "
                             "ELSE (SELECT count(*) FROM schema_migrations) END;")
            if existing not in ("", "0") and not args.replace_existing:
                raise RuntimeError("this box already holds an install (its database has migrations). "
                                   "Restore onto a clean server, or pass --replace-existing "
                                   "(a safety backup of what is here is taken first)")
        elif not args.replace_existing:
            warn("  ! MySQL/MariaDB: the operator's database server will be overwritten "
                 "(pass --replace-existing to confirm)")
            raise RuntimeError("refusing to overwrite a MySQL/MariaDB database without --replace-existing")

        if args.replace_existing:
            say("▶ Safety backup of what is about to be replaced...")
            # Never prunes: retention would otherwise delete same-day sets —
            # including, on 2026-10-16, the very set being restored.
            rc = cmd_backup(argparse.Namespace(dir=None, keep=None, quiet=True, no_prune=True,
                                               no_test_restore=True))
            if rc == 1:
                raise RuntimeError("the safety backup failed — nothing was replaced")
            steps.append(f"safety backup taken (exit {rc})")

        # 4. Vault, settings, databases, documents — in that order.
        if (x / "openbao.snap").is_file() and cfg.openbao_enabled:
            steps.append("vault: " + _restore_openbao(x, stamp))
            say("  ✓ " + steps[-1])
        elif (x / "openbao.snap").is_file():
            warn("  ! the set has a vault but OpenBao is not enabled here — vault not restored; .env is")
        envf = ROOT / ".env"
        if (x / "env").is_file():
            if envf.is_file():
                kept = _aside_dir(stamp) / "env"
                shutil.copy2(envf, kept)
                kept.chmod(0o600)
            shutil.copyfile(x / "env", envf)
            envf.chmod(0o600)
            steps.append(f".env restored (previous kept in {cfg.local_dir}/before-restore-{stamp}/)")
            say("  ✓ settings (.env)")
        cfg = Config()   # from here on, the RESTORED settings

        if backend == "postgresql":
            cid = container_of("velaris-db")
            _recreate_pg_db(cid, "velaris", "velaris", x / manifest["database"]["file"])
            # This server's database role was created with ITS password; the
            # restored .env carries the old install's. Make them agree.
            pw = cfg.env.get("VELARIS_DB_PASSWORD", "")
            if pw:
                _psql(cid, "velaris", "postgres", "ALTER ROLE velaris PASSWORD '" + pw.replace("'", "''") + "';")
                # A new password is a new SCRAM verifier; PgBouncer holds the one
                # it read when it started, and every server login failed after a
                # restore until it was restarted (clean box 2026-10-23). It reads
                # the verifier again when it starts.
                pcid = container_of("velaris-pooler")
                if pcid:
                    # Not fatal: the database is already restored; a pooler that
                    # did not restart is said, with the command, and the row check
                    # below does not use it.
                    if run(["docker", "restart", pcid], check=False).returncode == 0:
                        steps.append("pooler restarted (the role's new password)")
                    else:
                        warn("  ! the pooler did not restart — before starting the platform: "
                             "docker restart " + pcid)
        else:
            _restore_mysql(cfg, x / manifest["database"]["file"])
        steps.append(f"database ({backend}) restored")
        say("  ✓ database")
        # Row counts are compared NOW, before anything starts: the platform's
        # background jobs (chain sealing, health checks) write rows the moment it
        # runs, so a count taken after start differs from any backup, however good.
        tmpm = work / "manifest-for-counts.json"
        tmpm.write_text(json.dumps(manifest))
        rc_counts = _run_verifier(tmpm, ["--only", "row_counts"])
        rc_check = (rc_counts or {}).get("checks", [{}])[0] if rc_counts else {}
        if rc_check.get("ok") is False:
            raise RuntimeError("restored row counts do not match the set: " + rc_check.get("detail", ""))
        manifest["restore_row_counts"] = rc_check or {"ok": None, "detail": "could not run"}
        say(f"  ✓ row counts: {rc_check.get('detail', 'not checked')}")

        # Accounts purged AFTER this set was taken must not come back with it.
        merged, problems = collect_deletions(cfg)
        for pr in problems:
            warn(f"  ! deletion records: {pr} — the local copies are used")
        rep = replay_deletions(cfg)
        if rep.get("ok"):
            steps.append(f"deletion ledger: {len(rep.get('replayed', []))} purged again, "
                         f"{rep.get('already_gone', 0)} already gone ({merged} records fetched)")
            say("  ✓ " + steps[-1])
            for b in rep.get("refused_records", []):
                warn(f"  ! deletion record refused (does not verify): {b}")
        else:
            # An older set's schema: the platform replays them at its start, and
            # every sign-in check refuses these accounts until then.
            steps.append("deletion ledger: replayed when the platform starts")
            warn("  ! deletion ledger not replayed now (" + str(rep.get("error") or rep.get("failed"))[:160]
                 + ") — the platform does it at start; until then sign-in refuses those accounts")

        if manifest.get("temporal", {}).get("included") and "temporal" in cfg.external:
            steps.append("temporal: NOT restored — Temporal runs on another server here")
            warn("  ! temporal: not restored — it runs on another server here")
        elif manifest.get("temporal", {}).get("included"):
            run(compose("stop", "temporal", "temporal-ui"), check=False)
            tcid = container_of("temporal-db")
            _wait_pg(tcid, "temporal")
            for db in manifest["temporal"]["databases"]:
                _recreate_pg_db(tcid, "temporal", db, x / f"{db}.pgdump")
            tpw = cfg.env.get("VELARIS_TEMPORAL_DB_PASSWORD", "")
            if tpw:
                _psql(tcid, "temporal", "postgres", "ALTER ROLE temporal PASSWORD '" + tpw.replace("'", "''") + "';")
            run(compose("up", "-d", "temporal"), check=False)
            steps.append("Temporal workflows restored")
            say("  ✓ Temporal (in-flight timers and escalations)")

        steps.append("documents: " + _restore_documents(cfg, x, manifest, stamp, args.replace_existing))
        say("  ✓ " + steps[-1])

        rec = _recreate_containers(cfg)
        steps.append("containers: " + rec)
        if rec.startswith("NOT"):
            warn("  ! " + steps[-1])
        else:
            say("  ✓ " + steps[-1])

        mcopy = cfg.local_dir / LAST_MANIFEST
        mcopy.write_text(json.dumps({k: v for k, v in manifest.items() if k != "files"}, indent=2))
        mcopy.chmod(0o600)
        took = int(time.monotonic() - t0)
        say(f"✓ Restore finished in {took} s.")

        start_cmd = ("sudo systemctl start " + _installed_units()) if _systemd_managed() \
            else "./start-velaris.sh"
        if not args.start or _systemd_managed():
            if args.start:
                say("  This install runs under systemd; starting it needs sudo, which a restore does not hold.")
            say(f"  Next: {start_cmd}   then   ./restore-velaris.sh --verify")
            return 0
        say("▶ Starting the platform...")
        r = subprocess.run([str(ROOT / "start-velaris.sh")], stdout=subprocess.DEVNULL if QUIET else None)
        if r.returncode != 0:
            warn("✗ start-velaris.sh failed — run it by hand, then ./restore-velaris.sh --verify")
            return 1
        return cmd_verify(argparse.Namespace(gateway=True), started=t0)
    except Exception as exc:
        warn(f"✗ Restore FAILED — {exc}")
        if steps:
            warn("  done before the failure: " + "; ".join(steps))
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _restore_mysql(cfg: Config, dump: Path) -> None:
    e = cfg.env
    host = e.get("VELARIS_CASE_DB_HOST") or yaml_get("db_host") or "127.0.0.1"
    port = e.get("VELARIS_CASE_DB_PORT") or yaml_get("db_port") or "3306"
    name = e.get("VELARIS_CASE_DB_NAME") or yaml_get("db_name") or "velaris"
    user = e.get("VELARIS_CASE_DB_USER") or yaml_get("db_user") or "velaris"
    maria = cfg.db_backend == "mariadb"
    image, client = (MARIADB_CLIENT_IMAGE, "mariadb") if maria else (MYSQL_CLIENT_IMAGE, "mysql")
    env = {**os.environ, "MYSQL_PWD": e.get("VELARIS_DB_PASSWORD") or e.get("VELARIS_CASE_DB_PASSWORD") or ""}
    base = ["docker", "run", "--rm", "-i", "--network", "host", "-e", "MYSQL_PWD", image,
            client, "-h", host, "-P", str(port), "-u", user]
    run(base + ["-e", f"DROP DATABASE IF EXISTS `{name}`; CREATE DATABASE `{name}` "
                      "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"], env=env)
    with dump.open("rb") as fh:
        r = subprocess.run(base + [name], stdin=fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{client} import failed: " + r.stderr.decode(errors="replace")[-400:])


def _run_verifier(manifest_path: Path, extra: list[str]) -> dict | None:
    # Directly on the database, never through the pooler: a restore starts only
    # the databases, so through it every table read "missing" (clean box
    # 2026-10-23 — every restore of a pooled install failed its row check).
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "services" / "case-service"),
                                                         str(ROOT / "services" / "_shared")]),
           "VELARIS_CASE_DATABASE_POOLER_URL": "", "VELARIS_CASE_DATABASE_POOLER_SOCKET": "false"}
    r = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "backup" / "verify_restore.py"),
                        str(manifest_path), *extra], cwd=str(ROOT), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return json.loads(r.stdout.decode().strip().splitlines()[-1])
    except Exception:
        warn("  ! verifier could not run: " + r.stderr.decode(errors="replace")[-300:])
        return None


def cmd_verify(args, started: float | None = None) -> int:
    """Run verify_restore.py against the restored install."""
    import time
    cfg = Config()
    m = cfg.local_dir / LAST_MANIFEST
    if not m.is_file():
        warn(f"✗ no {LAST_MANIFEST} — run a restore first")
        return 1
    port = int(cfg.env.get("VELARIS_CASE_SERVICE_PORT") or 8200)
    extra = ["--skip", "row_counts"]   # compared at restore time, before start
    if getattr(args, "gateway", False) or _listening(port):
        extra += ["--gateway", f"http://127.0.0.1:{port}"]
    res = _run_verifier(m, extra)
    if res is None:
        return 1
    stored = json.loads(m.read_text()).get("restore_row_counts")
    if stored:
        res["checks"].insert(0, {"name": "row_counts", "ok": stored.get("ok"),
                                 "detail": "at restore time, before start: " + stored.get("detail", "")})
        res["ok"] = res["ok"] and stored.get("ok") is not False
    say("▶ Verification")
    for c in res["checks"]:
        mark = "–" if c["ok"] is None else ("✓" if c["ok"] else "✗")
        (say if c["ok"] is not False else warn)(f"  {mark} {c['name']}: {c['detail']}")
    rb = subprocess.run([str(ROOT / "scripts" / "secrets-render.sh"), "--check"], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT) if cfg.openbao_enabled else None
    if rb is not None:
        ok = rb.returncode == 0
        (say if ok else warn)(f"  {'✓' if ok else '✗'} vault: " + ("open, renders .env" if ok else
                              rb.stdout.decode(errors='replace').strip().splitlines()[0][:120]))
        res["ok"] = res["ok"] and ok
    if started is not None:
        say(f"  recovery time (restore + start + verify): {int(time.monotonic() - started)} s")
    say("✓ Verified." if res["ok"] else "✗ Verification FAILED — see above.")
    return 0 if res["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    global QUIET
    p = argparse.ArgumentParser(prog="velaris_backup")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--dir", help="local backup directory (default .backups/)")
    b.add_argument("--keep", help="legacy unencrypted dumps only: delete those older than N days")
    b.add_argument("--quiet", action="store_true")
    b.add_argument("--no-prune", action="store_true", help="take a set without applying retention")
    b.add_argument("--test-restore", action="store_true", help="prove this set restores, even if not due")
    b.add_argument("--no-test-restore", action="store_true", help="skip the restore test this run")
    k = sub.add_parser("ensure-key")
    k.add_argument("--recipient", help="use this age public key instead of generating a pair")
    k.add_argument("--no-push", action="store_true",
                   help="do not push to OpenBao (setup pushes the whole .env itself afterwards)")
    k.add_argument("--quiet", action="store_true")
    f = sub.add_parser("forget-private-key")
    f.add_argument("--quiet", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("set", help="a set file, or s3://bucket/key")
    r.add_argument("--key", required=True, help="the age private key file ('-' = stdin)")
    r.add_argument("--replace-existing", action="store_true",
                   help="allow replacing an install that is already here (a safety backup is taken first)")
    r.add_argument("--start", action="store_true", help="start the platform and verify afterwards")
    r.add_argument("--quiet", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--quiet", action="store_true")
    pd = sub.add_parser("push-deletions")
    pd.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)
    QUIET = bool(getattr(args, "quiet", False))
    return {"backup": cmd_backup, "ensure-key": cmd_ensure_key,
            "forget-private-key": cmd_forget_private_key, "restore": cmd_restore,
            "verify": cmd_verify, "push-deletions": cmd_push_deletions}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
