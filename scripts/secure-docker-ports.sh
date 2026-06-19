#!/usr/bin/env bash
#
# secure-docker-ports.sh — audit & harden Docker port exposure on a Velaris host.
#
# SAFE TO RUN BLIND: with no args it is READ-ONLY (audit). It never kills
# processes, never deletes data, and never recreates containers unless you pass
# --apply AND confirm. It degrades gracefully when sudo/tools are missing and
# never hangs waiting for a password.
#
# Born from the 2026-06-19 incident: temporal-db Postgres was published on
# 0.0.0.0:5433 with default creds, brute-forced, privilege-escalated
# (CVE-2024-10979 mat-view trick) and abused via `COPY ... FROM PROGRAM` to drop
# the PG_MEM / "Sofia" cryptojacker (binary /tmp/javavmx64).
#
# Usage:
#   ./secure-docker-ports.sh                 # AUDIT only (read-only, safe)
#   ./secure-docker-ports.sh --scan          # AUDIT + hunt malware IOCs (read-only)
#   ./secure-docker-ports.sh --apply         # recreate stack from hardened compose (asks to confirm)
#   ./secure-docker-ports.sh --apply --yes   # ...without the confirmation prompt
#   ./secure-docker-ports.sh --apply --firewall --yes   # also add a UFW backstop
#
# Env overrides: COMPOSE_DIR=/path/to/deploy/docker-compose
#
# NOTE: deliberately does NOT use `set -e` — a non-zero from a diagnostic command
# (e.g. grep finding nothing) must never abort the run. Real errors in --apply are
# handled explicitly.
set -uo pipefail

# ── locate the compose project ────────────────────────────────────────────────
_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
COMPOSE_DIR="${COMPOSE_DIR:-$(cd "$_self_dir/../deploy/docker-compose" 2>/dev/null && pwd || true)}"
COMPOSE_FILE="${COMPOSE_DIR:-.}/docker-compose.yml"

c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yel() { printf '\033[33m%s\033[0m\n' "$*"; }
hdr()   { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# Sensitive host ports that must never face the network.
SENSITIVE_PORTS='5432 5433 6379 9092 11434 9000 9001 1025 1143 8025 8233 9090 4317 4318'
# IOCs from the decoded dropper — process names / paths the malware uses.
IOCS='javavmx64|javavm64|/var/Sofia|pg_mem|mcrnlhoy|/tmp/mysql|/tmp/watchdog|/tmp/init|/tmp/\.r\.rpk|\.metabase|kdevtmpfsi|kinsing|xmrig'

# Run a privileged command WITHOUT ever prompting (sudo -n). Skips cleanly if it
# can't escalate, so the script never hangs when run blind/non-interactively.
PRIV=""
if [ "$(id -u)" = 0 ]; then PRIV="";
elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then PRIV="sudo -n";
else PRIV="__nopriv"; fi
run_priv() { if [ "$PRIV" = "__nopriv" ]; then return 1; else $PRIV "$@"; fi; }
have_priv() { [ "$PRIV" != "__nopriv" ]; }

# ── 1. PORT AUDIT (read-only) ─────────────────────────────────────────────────
audit_ports() {
  hdr "Published container ports (anything on 0.0.0.0 / :: is network-reachable)"
  local found=0
  while IFS='|' read -r name map; do
    [ -z "$name" ] && continue
    found=1
    case "$map" in
      127.0.0.1:*|::1:*|\[::1\]:*) c_grn "  loopback $name   $map" ;;
      0.0.0.0:*|:::*|\[::\]:*)     c_red "  EXPOSED  $name   $map" ;;
      *)                           printf '  %s   %s\n' "$name" "$map" ;;
    esac
  done < <(
    docker ps --format '{{.Names}}' 2>/dev/null | while read -r n; do
      docker inspect -f '{{$n:=.Name}}{{range $p,$conf := .NetworkSettings.Ports}}{{if $conf}}{{range $conf}}{{$n}}|{{.HostIp}}:{{.HostPort}}->{{$p}}{{"\n"}}{{end}}{{end}}{{end}}' "$n" 2>/dev/null
    done | sed 's#^/##' | grep -- '->' | sort
  )
  [ "$found" = 0 ] && c_grn "  (no published ports — everything is Docker-network-only)"
  echo
  c_yel "  Databases/caches/brokers/admin-UIs should read 'loopback' or have no"
  c_yel "  mapping. Only a TLS-terminated public ingress should be EXPOSED."
}

# ── 2. FIREWALL POSTURE ───────────────────────────────────────────────────────
check_firewall() {
  hdr "Firewall posture"
  echo "  NOTE: Docker writes its own iptables rules and BYPASSES UFW for published"
  echo "  ports. A 0.0.0.0 mapping is reachable even if 'ufw status' looks locked."
  echo "  The port BINDING (--apply) is the real fix; UFW is only a backstop."
  echo
  if command -v ufw >/dev/null 2>&1; then
    if have_priv; then run_priv ufw status verbose 2>/dev/null | sed 's/^/  /'
    else echo "  (need root to read ufw status; re-run with sudo to see it)"; fi
  else echo "  (ufw not installed)"; fi

  hdr "Listening sockets reachable off-host"
  if command -v ss >/dev/null 2>&1; then
    ss -tln 2>/dev/null | awk 'NR==1 || ($4 !~ /127\.0\.0\.1|::1|\[::1\]/)' | sed 's/^/  /'
  else echo "  (ss not available)"; fi
}

# ── 3. IOC SCAN (read-only) ───────────────────────────────────────────────────
scan_iocs() {
  hdr "Host process scan for known cryptojacker IOCs"
  if ps aux 2>/dev/null | grep -E "$IOCS" | grep -v grep; then c_red "  ^ SUSPICIOUS processes on host"; else c_grn "  none on host"; fi

  hdr "Suspicious files in temp dirs"
  ls -la /tmp /var/tmp /dev/shm 2>/dev/null | grep -Ei "$IOCS" | sed 's/^/  /' || c_grn "  clean"

  hdr "Persistence (cron) review"
  { crontab -l 2>/dev/null; have_priv && run_priv cat /etc/crontab /etc/cron.d/* 2>/dev/null; have_priv && run_priv ls -la /var/spool/cron/crontabs 2>/dev/null; } \
    | grep -Ei "$IOCS|/dev/tcp|base64 -d|curl .*sh|wget .*sh" | sed 's/^/  /' || c_grn "  no obviously malicious cron entries (run with sudo for full coverage)"

  hdr "Outbound connections (possible mining-pool traffic)"
  if command -v ss >/dev/null 2>&1; then
    ss -tun 2>/dev/null | grep ESTAB | grep -vE ':22|:443|:80|127\.0\.0\.1|::1' | sed 's/^/  /' || c_grn "  none notable"
  else echo "  (ss not available)"; fi

  hdr "Per-container scan (processes + injected Postgres objects)"
  docker ps --format '{{.Names}}' 2>/dev/null | while read -r n; do
    [ -z "$n" ] && continue
    hits=$(docker top "$n" 2>/dev/null | grep -E "$IOCS")
    [ -n "$hits" ] && { c_red "  [$n] suspicious process:"; echo "$hits" | sed 's/^/    /'; }
    # Auto-detect the Postgres superuser from the container env (temporal/helix/…).
    pguser=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$n" 2>/dev/null | sed -n 's/^POSTGRES_USER=//p' | head -1)
    if [ -n "$pguser" ]; then
      rogue=$(timeout 10 docker exec "$n" psql -U "$pguser" -tAc \
        "SELECT 'fn:'||proname FROM pg_proc WHERE proname IN ('attack','conv','get_target')
         UNION ALL SELECT 'tbl:'||relname FROM pg_class WHERE relname IN ('tests','remove_later');" 2>/dev/null)
      [ -n "$rogue" ] && { c_red "  [$n] INJECTED DB OBJECTS — treat this database as compromised:"; echo "$rogue" | sed 's/^/    /'; }
    fi
  done
  echo; c_grn "  IOC scan complete."
}

# ── 4. APPLY HARDENING ────────────────────────────────────────────────────────
# Heuristic: count sensitive ports in the target compose that are NOT loopback-bound.
compose_still_exposes() {
  [ -f "$COMPOSE_FILE" ] || return 2
  local n=0 p
  for p in $SENSITIVE_PORTS; do
    if grep -E "(^|[^0-9.])${p}:[0-9]+" "$COMPOSE_FILE" 2>/dev/null | grep -vq "127\.0\.0\.1:${p}:"; then
      grep -E "(\"|- \")${p}:[0-9]+" "$COMPOSE_FILE" 2>/dev/null | grep -vq "127\.0\.0\.1" && { c_red "    still exposes :$p"; n=$((n+1)); }
    fi
  done
  return "$n"
}

apply_hardening() {
  hdr "Apply hardened compose: $COMPOSE_FILE"
  if [ ! -f "$COMPOSE_FILE" ]; then
    c_red "  Compose file not found. Copy the hardened repo here first, or set COMPOSE_DIR=..."
    return 1
  fi
  # Validate it parses before touching anything.
  if ! ( cd "$COMPOSE_DIR" && docker compose config -q 2>/tmp/_compose_err ); then
    c_red "  Compose file is invalid — NOT applying:"; sed 's/^/    /' /tmp/_compose_err; rm -f /tmp/_compose_err; return 1
  fi
  rm -f /tmp/_compose_err
  # Warn loudly if the target compose would re-expose databases.
  c_yel "  Checking the target compose is actually hardened…"
  compose_still_exposes; local exposed=$?
  if [ "$exposed" -gt 0 ]; then
    c_red "  WARNING: this compose still publishes $exposed sensitive port(s) on all interfaces."
    c_red "  Applying it would NOT fix the exposure. Pull the hardened compose first."
    if [ "$ASSUME_YES" != 1 ]; then c_red "  Aborting (use --yes to override)."; return 1; fi
  else
    c_grn "  OK: target compose binds all sensitive ports to loopback or none."
  fi
  # Snapshot current published ports for rollback reference.
  local snap="/tmp/docker-ports-before-$(date +%Y%m%d-%H%M%S).txt"
  docker ps --format '{{.Names}} {{.Ports}}' >"$snap" 2>/dev/null && c_grn "  Snapshot of current ports saved: $snap"
  # Confirm unless --yes; refuse to act blind without a TTY.
  if [ "$ASSUME_YES" != 1 ]; then
    if [ ! -t 0 ]; then c_red "  Non-interactive and no --yes given; refusing to recreate containers. Aborting."; return 1; fi
    printf '\033[1m  Recreate the stack now with `docker compose up -d --remove-orphans`? [y/N] \033[0m'
    read -r ans; case "$ans" in y|Y|yes|YES) ;; *) c_yel "  Cancelled."; return 0;; esac
  fi
  ( cd "$COMPOSE_DIR" && docker compose up -d --remove-orphans ) || { c_red "  docker compose up failed."; return 1; }
  c_grn "  Recreated. Re-auditing ports:"
  audit_ports
}

install_firewall_backstop() {
  hdr "UFW backstop: deny sensitive ports from off-host"
  command -v ufw >/dev/null 2>&1 || { c_yel "  ufw not installed; skipping."; return 0; }
  have_priv || { c_yel "  need root for ufw; re-run with sudo. Skipping."; return 0; }
  local p
  for p in $SENSITIVE_PORTS; do
    run_priv ufw deny "$p"/tcp >/dev/null 2>&1 && echo "  denied $p/tcp"
  done
  c_yel "  Backstop only — Docker can still bypass UFW for 0.0.0.0 ports, so the"
  c_yel "  binding fix (--apply) remains the primary control."
}

# ── main ──────────────────────────────────────────────────────────────────────
APPLY=0; FIREWALL=0; SCAN=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --apply)    APPLY=1 ;;
    --firewall) FIREWALL=1 ;;
    --scan)     SCAN=1 ;;
    --yes|-y)   ASSUME_YES=1 ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) c_red "unknown arg: $a (try --help)"; exit 1 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then c_red "docker not found in PATH."; exit 1; fi
if ! docker info >/dev/null 2>&1; then c_red "cannot talk to the Docker daemon (need access / sudo?)."; exit 1; fi
[ "$PRIV" = "__nopriv" ] && c_yel "(running without root: sudo-only checks will be skipped, never prompted)"

audit_ports
check_firewall
[ "$SCAN" = 1 ]     && scan_iocs
[ "$APPLY" = 1 ]    && apply_hardening
[ "$FIREWALL" = 1 ] && install_firewall_backstop

hdr "Done"
[ "$APPLY" = 0 ] && c_yel "Audit only (read-only). Add --scan to hunt malware, --apply to recreate the stack."
exit 0
