#!/usr/bin/env bash
# Copyright (c) Velaris. All rights reserved.
#
# Monthly image check (Nov_TODO R5e, 2026-10-16).
#
# Lists every pinned image in the compose file and the service Dockerfiles beside
# the newest versions published on Docker Hub (or GHCR for ghcr.io images): first in the pin's own line
# (same major.minor), then the newest overall. It changes nothing — raising a
# version is a deliberate edit, tested with that month's release.
#
#   ./scripts/check-image-versions.sh
#
# Read-only. Needs python3 (standard library only); no Docker Hub login.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

{
  grep -hE '^\s*image:' "$ROOT/deploy/docker-compose/docker-compose.yml" | awk '{print $2}'
  grep -hE '^FROM ' "$ROOT"/services/*/Dockerfile "$ROOT"/engine/Dockerfile 2>/dev/null | awk '{print $2}'
  # Images the scripts run directly (docker run …), outside the compose file.
  grep -hoE '(mysql|mariadb):[0-9]+\.[0-9]+[0-9.]*|(postgres|python):[0-9]+\.[0-9.]+-alpine' \
    "$ROOT"/setup-velaris.sh "$ROOT"/start-velaris.sh "$ROOT"/scripts/backup/velaris_backup.py \
    "$ROOT"/deploy/external-services.sh 2>/dev/null
} | grep ':' | sort -u | python3 -I -c '
import json, re, sys, urllib.request, urllib.parse

BAD = re.compile(r"rc|beta|alpha|fips|rocm|arm64|amd64|386|perl|otel|windows")

def ghcr_tags(repo, name=""):
    # GitHub Container Registry (the pooler image): anonymous pull token, then
    # the registry tag list. No ordering or name filter there, so filter here.
    try:
        tok = urllib.parse.urlencode({"scope": f"repository:{repo}:pull"})
        with urllib.request.urlopen(f"https://ghcr.io/token?{tok}", timeout=20) as r:
            token = json.load(r)["token"]
        req = urllib.request.Request(f"https://ghcr.io/v2/{repo}/tags/list?n=1000",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return [t for t in json.load(r).get("tags") or [] if t.startswith(name)]
    except Exception:
        return None

def tags(hub, name=""):
    if hub.startswith("ghcr.io/"):
        return ghcr_tags(hub[len("ghcr.io/"):], name)
    q = urllib.parse.urlencode({"page_size": 100, "ordering": "last_updated", "name": name})
    url = f"https://hub.docker.com/v2/repositories/{hub}/tags?{q}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return [t["name"] for t in json.load(r).get("results", [])]
    except Exception:
        return None

def ver(n):
    return tuple(int(x) for x in re.findall(r"[0-9]+", n.split("-")[0]))

print("%-48s %-22s %s" % ("PINNED", "SAME LINE", "NEWEST"))
for img in sys.stdin.read().split():
    repo, tag = img.rsplit(":", 1)
    hub = repo if "/" in repo else f"library/{repo}"
    if tag.startswith("RELEASE."):                      # MinIO: dated releases
        t = tags(hub, "RELEASE.")
        newest = sorted(n for n in (t or []) if re.fullmatch(r"RELEASE\.[0-9TZ-]+", n))[-1:]
        why = "(not on Docker Hub)" if t is None else "(no releases published)"
        print("%-48s %-22s %s" % (img, "-", newest[0] if newest else why))
        continue
    m = re.match(r"^(v?)([0-9]+)\.([0-9]+)(\.[0-9]+)?", tag)
    if not m:
        print("%-48s (not a version pin)" % img); continue
    suffix = re.sub(r"^v?[0-9.]+", "", tag)            # e.g. -alpine, -slim
    def ok(n):
        return (re.match(r"^v?[0-9]+\.[0-9]+(\.[0-9]+){0,2}" + re.escape(suffix) + r"$", n)
                and not BAD.search(n))
    # The pin line: major.minor for x.y.z pins, major for x.y pins (postgres 16.15).
    line = tags(hub, m.group(1) + m.group(2) + ("." + m.group(3) if m.group(4) else ""))
    allt = tags(hub)
    if line is None or allt is None:
        print("%-48s (could not read)" % img); continue
    pre = m.group(1) + m.group(2) + ("." + m.group(3) if m.group(4) else "") + "."
    same = max((n for n in line if ok(n) and n.startswith(pre)), key=ver, default="-")
    new = max((n for n in allt if ok(n)), key=ver, default="-")
    flag = "   <- newer exists" if new != "-" and ver(new) > ver(tag) else ""
    print("%-48s %-22s %s%s" % (img, same, new, flag))
'
