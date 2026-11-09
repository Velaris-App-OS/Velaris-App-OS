#!/usr/bin/env python3
"""Support bundle — what a customer sends when they report a fault (R7).

    ./support-bundle.sh                 → velaris-support-<host>-<time>.tar.gz
    ./support-bundle.sh --out /tmp      → written there instead
    ./support-bundle.sh --lines 2000    → more log lines per service (default 500)

What goes in: the version, the OS, disk and memory, which containers and
services are running, the health check (Studio > Status), the NAMES of the
settings in .env (never a value), and the last lines of each service's log.

What never goes in: .env values, keys, certificates, the database, documents,
the install report. And every line of every file passes `redact()` before it is
written: e-mail addresses, IP addresses, phone numbers, tokens and JWTs, URL
passwords, `password=` / `secret:` / `token` / `key` style values, and long
hex or base64 strings are replaced. A case title or a person's name typed into
free text cannot be recognised by a pattern — so the bundle is plain text, its
path is printed, and the customer is asked to look before sending it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import io
import os
import platform
import re
import socket
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_RULES: list[tuple[re.Pattern, str]] = [
    # credentials in URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    # Authorization headers and bearer tokens
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), "[JWT]"),
    # PEM blocks (one line each, after splitting)
    (re.compile(r"-----BEGIN [A-Z ]+-----.*"), "[PEM]"),
    # key=value / key: value where the key names a secret
    (re.compile(r"(?i)\b([\w.-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|"
                r"credential|authorization|cookie|session[_-]?id|otp|kek|dek)[\w.-]*)"
                r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',;&]+)"), r"\1\2[REDACTED]"),
    # e-mail addresses
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    # IPv4 (loopback kept: it says "local", nothing about anyone)
    (re.compile(r"\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    # IPv6: hex groups with a "::", or three or more colons and a hex letter —
    # so a clock time like 12:30:45 (no letter, no "::") survives
    (re.compile(r"(?<![\w:])(?=[0-9A-Fa-f:]*::|(?:[0-9A-Fa-f]*:){3}[0-9A-Fa-f:]*[A-Fa-f])"
                r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"), "[IP]"),
    # phone numbers: + and 9-15 digits, spaces or dashes allowed
    (re.compile(r"\+\d[\d\s-]{8,17}\d"), "[PHONE]"),
    # long hex / base64 runs: keys, hashes, tokens (a UUID is 8-4-4-4-12 and survives)
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[HEX]"),
    # a long run with a digit in it: a token or key. No "/": a file path in a
    # traceback is the most useful line in a bundle and holds nobody's data.
    (re.compile(r"\b(?=[A-Za-z0-9+_-]*\d)[A-Za-z0-9+_-]{40,}={0,2}"), "[BLOB]"),
    # an account name inside a home directory path
    (re.compile(r"(/home/|/Users/)[^/\s]+"), r"\1[USER]"),
]


def redact(text: str) -> str:
    out = []
    for line in text.splitlines():
        for rx, rep in _RULES:
            line = rx.sub(rep, line)
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
    except Exception as exc:  # noqa: BLE001
        return f"[could not run {cmd[0]}: {type(exc).__name__}]\n"


def setting_names(env_file: Path) -> str:
    """The names of the settings this install sets — never a value."""
    if not env_file.is_file():
        return "[no .env]\n"
    names = []
    for line in env_file.read_text(errors="replace").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            names.append(m.group(1))
    return "\n".join(sorted(set(names))) + "\n"


def version() -> str:
    try:
        for line in (ROOT / "velaris.yaml").read_text().splitlines():
            m = re.match(r"^\s*version:\s*[\"']?([^\"'#\s]+)", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return "unknown"


def logs(lines: int) -> dict[str, str]:
    out: dict[str, str] = {}
    units = _run(["systemctl", "list-units", "velaris-*", "--type=service", "--no-legend",
                  "--plain"]).split()
    for unit in [u for u in units if u.startswith("velaris-") and u.endswith(".service")]:
        out[f"logs/{unit}.log"] = _run(["journalctl", "-u", unit, "-n", str(lines),
                                        "--no-pager", "-o", "short-iso"])
    log_dir = Path(os.environ.get("VELARIS_LOG_DIR") or ROOT / "logs")
    if log_dir.is_dir():
        for f in sorted(log_dir.glob("velaris-*.log")):
            try:
                tail = f.read_text(errors="replace").splitlines()[-lines:]
                out[f"logs/{f.name}"] = "\n".join(tail) + "\n"
            except OSError:
                continue
    return out


def health() -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT}/services/case-service:{ROOT}/services/_shared"
    py = ROOT / ".venv/bin/python"
    if not py.exists():
        return "[no .venv]\n"
    try:
        r = subprocess.run([str(py), "-m", "case_service.readiness"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=90)
        return r.stdout or "[no output]\n"
    except Exception as exc:  # noqa: BLE001
        return f"[could not check: {type(exc).__name__}]\n"


def collect(lines: int) -> dict[str, str]:
    files = {
        "README.txt": (
            "Velaris support bundle\n"
            f"made:    {datetime.now(timezone.utc).isoformat()}\n"
            f"version: {version()}\n\n"
            "Every file here passed the redaction in scripts/support_bundle.py: e-mail,\n"
            "IP addresses, phone numbers, tokens, passwords and long keys are replaced.\n"
            "Names or case details typed into free text cannot be recognised by a\n"
            "pattern — please look through logs/ before you send this.\n"
            "Not included, ever: .env values, keys, certificates, the database, documents.\n"),
        "system.txt": (f"os: {platform.platform()}\npython: {platform.python_version()}\n\n"
                       + _run(["sh", "-c", "cat /etc/os-release 2>/dev/null; echo; uptime; echo; "
                                     "free -m; echo; df -h"])),
        "containers.txt": _run(["docker", "ps", "-a", "--format",
                                "{{.Names}}\t{{.Image}}\t{{.Status}}"]),
        "services.txt": _run(["systemctl", "list-units", "velaris-*", "--all", "--no-legend",
                              "--plain"]),
        "health.txt": health(),
        "settings-names.txt": setting_names(ROOT / ".env"),
    }
    files.update(logs(lines))
    return {name: redact(text) for name, text in files.items()}


def write(files: dict[str, str], out_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname())[:40] or "host"
    name = f"velaris-support-{host}-{stamp}"
    path = out_dir / f"{name}.tar.gz"
    old = os.umask(0o077)
    try:
        with tarfile.open(path, "w:gz") as tar:
            for rel, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(f"{name}/{rel}")
                info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
                tar.addfile(info, io.BytesIO(data))
    finally:
        os.umask(old)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Make a support bundle (no secrets, no personal data).")
    ap.add_argument("--out", default=str(ROOT), help="directory to write it to")
    ap.add_argument("--lines", type=int, default=500, help="log lines per service")
    a = ap.parse_args(argv)
    path = write(collect(max(10, min(a.lines, 20000))), Path(a.out))
    print(f"Support bundle: {path}")
    print("Look through it before sending — names typed into cases cannot be recognised "
          "by a pattern.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
