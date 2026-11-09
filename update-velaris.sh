#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Update Script
#  Checks for a new release, backs up the DB, pulls new code/images,
#  runs migrations, and restarts all services.
#
#  Usage:  ./update-velaris.sh [--yes]   (--yes skips confirmation)
#
#  ── Switching update source ─────────────────────────────────────
#  Currently using GitHub Releases API. When moving to your own
#  server, change UPDATE_SOURCE to "server" and set UPDATE_SERVER_URL.
#  Everything else stays the same.
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# R5c: a server linked as a STANDBY runs only its two replica databases
# (standby-velaris.sh). Starting the platform here would start a second
# PostgreSQL on the replicas' own data. standby.json exists only while linked.
if [ -f "$VELARIS_DIR/.standby/standby.json" ]; then
  echo "✗ This server is linked as a Velaris standby ($VELARIS_DIR/.standby/standby.json)." >&2
  echo "  The platform must not run here while it is one. To use it on its own, unlink it" >&2
  echo "  first: ./standby-velaris.sh unlink   (on the main server)" >&2
  exit 1
fi
# An old main whose standby was promoted: running it again means two servers
# taking writes. demoted.json stays until it is linked as the new main's standby.
if [ -f "$VELARIS_DIR/.standby/demoted.json" ]; then
  echo "✗ This server was replaced by its standby ($VELARIS_DIR/.standby/demoted.json)." >&2
  echo "  Starting it would make two main servers. To bring it back as a standby:" >&2
  echo "  ./standby-velaris.sh retire   then link it from the new main." >&2
  exit 1
fi
# Step 12 (automatic failover): this main stopped itself after losing both the
# witness and its standby — the standby may have taken over. Its agent lifts
# this by itself once the witness confirms this server still holds the lease.
if [ -f "$VELARIS_DIR/.standby/fenced.json" ]; then
  echo "✗ This server stopped itself: it lost both the failover witness and its standby" >&2
  echo "  ($VELARIS_DIR/.standby/fenced.json). The standby may have taken over." >&2
  echo "  Check: ./standby-velaris.sh failover status   (it restarts by itself if it still holds the lease)" >&2
  exit 1
fi

# Run by the timer, this script gets systemd's bare PATH and no terminal.
#  - `uv` normally lives in ~/.local/bin, which that PATH lacks: the dependency
#    sync was being skipped as "uv not found" on every timer-driven update.
#    Appended, so nothing an operator has on PATH is overridden.
#  - git must fail at once rather than wait for a passphrase or password nobody
#    is there to type. With a terminal attached nothing changes.
export PATH="$PATH${HOME:+:$HOME/.local/bin:$HOME/.cargo/bin}"
if [ ! -t 0 ]; then
  export GIT_TERMINAL_PROMPT=0
  export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o BatchMode=yes"
fi
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
# Which optional compose services this install runs (the bundled MinIO is kept
# only where it already exists) — see the file for the rule.
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh" ] && . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"
DB_CONTAINER="docker-compose-velaris-db-1"
DB_USER="velaris"
DB_NAME="velaris"
# Database calls go through deploy/external-services.sh (bundled container, or a
# database on another server over TLS). A tree without that file — the release
# this one replaces — gets the bundled calls it always made.
if ! type velaris_psql >/dev/null 2>&1; then
  velaris_is_external() { return 1; }
  velaris_psql()        { docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" "$@"; }
  velaris_pg_dump()     { docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" "$@"; }
  velaris_pg_isready()  { docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -q; }
fi
# Parts on other servers must be encrypted and authenticated: refuse before
# anything is touched (nothing listed = nothing checked).
if type velaris_external_check >/dev/null 2>&1 && ! velaris_external_check; then
  exit 1
fi
BACKUP_DIR="$VELARIS_DIR/.backups"
# Scratch files and error logs go to a directory only this user can write. A
# fixed name under /tmp can be pre-created, or pointed somewhere else, by any
# other local user before this script writes to it.
VELARIS_SCRATCH="${VELARIS_SCRATCH:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/velaris}"
if ! { mkdir -p "$VELARIS_SCRATCH" && chmod 700 "$VELARIS_SCRATCH" && [ -w "$VELARIS_SCRATCH" ]; } 2>/dev/null; then
  VELARIS_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/velaris.XXXXXX")"
fi

# ── UPDATE SOURCE CONFIG — read from velaris.yaml ───────────────
# To switch from GitHub to your own server, edit velaris.yaml:
#   updates.source: "server"
#   updates.server_url: "https://updates.velaris.io/latest"
_read_yaml() {
  local key="$1" default="${2:-}"
  grep -E "^\s+${key}:" "$VELARIS_DIR/velaris.yaml" 2>/dev/null \
    | head -1 \
    | sed -E "s/.*${key}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" \
    | xargs \
    || echo "$default"
}
UPDATE_SOURCE=$(_read_yaml "source" "github")
GITHUB_REPO=$(_read_yaml "github_repo" "your-org/velaris")
UPDATE_SERVER_URL=$(_read_yaml "server_url" "https://updates.velaris.io/latest")
UPDATE_CHANNEL=$(_read_yaml "channel" "stable")
AUTO_UPDATE=$(_read_yaml "auto_update" "false")
UPDATE_WINDOW=$(_read_yaml "update_window" "")
MANIFEST_BRANCH=$(_read_yaml "manifest_branch" "main")

# ── Backend ───────────────────────────────────────────────────────
# PostgreSQL (bundled, or on another server) or the MySQL family (MySQL 8,
# MariaDB 10.6+/11 — bring-your-own servers, one migrations/mysql track). Until
# 2026-11-01 this updater refused the MySQL family outright ("apply schema
# changes manually"): two of the three supported databases had no update path
# at all (R3). The MySQL calls are deploy/external-services.sh's velaris_mysql
# and velaris_mysqldump — the same client setup and start use.
_DB_BACKEND=$(_read_yaml "database" "postgresql")
case "$_DB_BACKEND" in
  postgresql)    DB_FAMILY=postgresql ;;
  mysql|mariadb) DB_FAMILY=mysql ;;
  *) echo "ERROR: unsupported database '$_DB_BACKEND' in velaris.yaml (postgresql, mysql, mariadb)." >&2; exit 1 ;;
esac
if [ "$DB_FAMILY" = mysql ] && ! type velaris_mysql >/dev/null 2>&1; then
  # The tree being updated predates deploy/external-services.sh's MySQL calls
  # (3.1.2: its own updater refused MySQL, so its operator runs THIS file by
  # hand once). The same calls, minus TLS — that release had none for MySQL.
  _vx_yaml() { local v; v="$(_read_yaml "$1" "")"; printf '%s' "${v:-$2}"; }
  _vx_mysql_run() {
    local tool="$1"; shift; local -a t=(); [ "$tool" = mysql ] && t=(--connect-timeout=10)
    MYSQL_PWD="$(grep -E '^VELARIS_DB_PASSWORD=' "$VELARIS_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"')" \
      docker run --rm -i --network host -e MYSQL_PWD \
      mysql:8.4.11 "$tool" "${t[@]}" -h "$(_vx_yaml db_host 127.0.0.1)" -P "$(_vx_yaml db_port 3306)" \
      -u "$(_vx_yaml db_user velaris)" "$@"
  }
  velaris_mysql()     { _vx_mysql_run mysql "$(_vx_yaml db_name velaris)" "$@"; }
  velaris_mysqldump() { _vx_mysql_run mysqldump --single-transaction --routines --triggers --no-tablespaces \
                          --column-statistics=0 "$(_vx_yaml db_name velaris)"; }
fi

# PUO Phase 1: UI-approved update request written by Studio (platform_updates.py)
# Phase 2: requests may carry scheduled_for (next after-hours slot from the
# /admin business calendar) — the agent waits for that exact moment.
UPDATE_REQUEST_FILE="$VELARIS_DIR/.update-request"
REQUEST_MODE=""
REQUEST_SCHEDULED_FOR=""
REQUEST_ACTION=""
REQUEST_TO_VERSION=""
if [[ -f "$UPDATE_REQUEST_FILE" ]]; then
  REQUEST_MODE=$(python3 -c "import json; print(json.load(open('$UPDATE_REQUEST_FILE')).get('mode','window'))" 2>/dev/null || echo "window")
  REQUEST_SCHEDULED_FOR=$(python3 -c "import json; print(json.load(open('$UPDATE_REQUEST_FILE')).get('scheduled_for') or '')" 2>/dev/null || echo "")
  REQUEST_ACTION=$(python3 -c "import json; print(json.load(open('$UPDATE_REQUEST_FILE')).get('action') or '')" 2>/dev/null || echo "")
  REQUEST_TO_VERSION=$(python3 -c "import json; print(json.load(open('$UPDATE_REQUEST_FILE')).get('to_version') or '')" 2>/dev/null || echo "")
fi

# Service port for the post-update health gate (default 8200)
SERVICE_PORT=$(grep -E "^VELARIS_CASE_SERVICE_PORT=" "$VELARIS_DIR/.env" 2>/dev/null | cut -d= -f2 || true)
SERVICE_PORT="${SERVICE_PORT:-8200}"

# ─────────────────────────────────────────────────────────────────

green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
red()    { echo -e "\033[0;31m$*\033[0m"; }

# ── System binaries, checked BEFORE anything is touched ──────────
#
# The update syncs Python and Node dependencies and pulls container images. It
# cannot install an apt package, so a release that starts using a new system
# tool would install cleanly on a NEW machine and break on every existing one —
# at runtime, in whichever component called it first.
#
# It halts rather than installing: apt needs root, this runs unattended on a
# timer, and a package manager running unwatched on a client's server is not a
# decision an update gets to make.
if [ -f "$VELARIS_DIR/deploy/deps/velaris-deps.sh" ]; then
  # shellcheck source=/dev/null
  . "$VELARIS_DIR/deploy/deps/velaris-deps.sh"
  _MISSING_PKGS="$(_velaris_deps_missing)"
  if [ -n "$_MISSING_PKGS" ]; then
    red "✗ This release needs system packages that are not installed:"
    _velaris_deps_report
    red "  Nothing has been changed. Install them and re-run the update:"
    red "    sudo apt-get update && sudo apt-get install -y $_MISSING_PKGS"
    exit 1
  fi
fi

# ── Ports, checked BEFORE anything is touched ────────────────────
#
# An update can add a component, and a component needs a port the client may
# already be using. Finding that out after the pull, the migrations and the
# restart means diagnosing a half-applied update; finding it out here costs
# nothing. Ports held by Velaris itself are expected — the platform is running
# while this runs — so only a port held by something ELSE stops us.
#
# This never prompts and never reassigns: the timer runs this unattended, and a
# port chosen by a script at 3am is a port nobody knows about.
if [ -f "$VELARIS_DIR/deploy/ports/velaris-ports.sh" ]; then
  # shellcheck source=/dev/null
  set -a; . "$VELARIS_DIR/.env" 2>/dev/null || true; set +a
  . "$VELARIS_DIR/deploy/ports/velaris-ports.sh"
  if ! _velaris_ports_preflight --quiet; then
    red "✗ A port this release needs is held by something that is not Velaris (above)."
    red "  Nothing has been changed. Set the port in .env and re-run the update."
    exit 1
  fi
fi

# ── Gateway limits, checked BEFORE anything is touched ───────────
#
# The gateway's rate and connection ceilings are substituted into
# nginx.conf.template from .env (2026-09-09). nginx validates them at START,
# not at write time, so a typo here is not a bad limit — it is a gateway that
# refuses to boot, which is the whole platform. That failure has happened once
# already (an empty identity secret, 2026-08-24), so it is checked here where
# it costs nothing and nothing has been touched yet.
#
# UNSET OR EMPTY IS ALWAYS FINE: compose supplies the shipped default, which is
# the ceiling the gateway already enforces. Only a value the operator actually
# typed can be wrong.
_velaris_check_gateway_limits() {
  local bad=0 v
  for k in VELARIS_GW_RATE_API VELARIS_GW_RATE_PORTAL; do
    eval "v=\${$k:-}"
    [ -z "$v" ] && continue
    if ! printf '%s' "$v" | grep -Eq '^[1-9][0-9]*r/[sm]$'; then
      red "  ✗ $k=\"$v\" is not an nginx rate (expected e.g. 20r/s or 30r/m)"
      bad=1
    fi
  done
  v="${VELARIS_GW_WS_CONN_MAX:-}"
  if [ -n "$v" ] && ! printf '%s' "$v" | grep -Eq '^[1-9][0-9]*$'; then
    red "  ✗ VELARIS_GW_WS_CONN_MAX=\"$v\" is not a positive whole number"
    bad=1
  fi
  return $bad
}
if ! _velaris_check_gateway_limits; then
  red "✗ A gateway limit in .env would stop nginx from starting."
  red "  Nothing has been changed. Correct the value, or delete the line to"
  red "  take the shipped default, and re-run."
  exit 1
fi
bold()   { echo -e "\033[1m$*\033[0m"; }
_CURRENT_STEP=""
step()   { _CURRENT_STEP="$*"; echo ""; bold "▶ $*"; }

# ── Version beacon — fleet visibility for ops ────────────────────
# previous_version is the rollback target — carry it across beacon writes
# so an hourly "current" status never erases it.
PREVIOUS_VERSION=$(python3 -c "import json; print(json.load(open('$VELARIS_DIR/.update-status')).get('previous_version') or '')" 2>/dev/null || echo "")
_STATUS_WRITTEN=false
_write_status() {
  # args: result message
  _STATUS_WRITTEN=true
  cat > "$VELARIS_DIR/.update-status" <<EOF
{"version": "${CURRENT_VERSION:-unknown}", "previous_version": "${PREVIOUS_VERSION}", "channel": "$UPDATE_CHANNEL", "result": "$1", "message": "$2", "timestamp": "$(date -Iseconds)"}
EOF
}

# ── An update that stops must say so ─────────────────────────────
# `set -e` ends this script at the first failing command, and until 2026-10-12
# it could do that halfway through with no message and no beacon: the code
# checked out, the migrations not applied, `.update-status` still describing
# the run before. Once the update proper has begun, any exit that is not a
# success and has not already written its own status writes one here, naming
# the step. The marker is left in place so a re-run finishes the job.
UPDATE_MARKER_FILE="$VELARIS_DIR/.update-in-progress"
_IN_UPDATE=false
_on_exit() {
  local rc=$?
  [[ -n "${MANIFEST_FILE:-}" ]] && rm -f "$MANIFEST_FILE" "${MANIFEST_FILE}.sig"
  if [[ "$rc" -ne 0 && "$_IN_UPDATE" == true && "$_STATUS_WRITTEN" == false ]]; then
    red ""
    red "  ✗ The update STOPPED at: ${_CURRENT_STEP:-unknown step} (exit $rc)"
    red "    It is NOT complete. Fix the cause above, then run ./update-velaris.sh again —"
    red "    it will pick up where this run stopped."
    _write_status "failed" "stopped at ${_CURRENT_STEP:-unknown step} (exit $rc) - update to v${LATEST_VERSION:-?} not complete"
    rm -f "$UPDATE_REQUEST_FILE"   # consumed — never auto-retry a failing update
  fi
}
trap _on_exit EXIT

# A version string arrives from OUTSIDE this script — the channel manifest, or
# `to_version` in a rollback request written by the web tier — and is then put
# into a `git checkout`, a `sed` expression, a `python3 -c` string and the JSON
# beacon. Only a plain version may get that far: GNU sed alone would run a
# command for anything carrying `/e`.
_valid_version() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; }
_require_valid_version() {
  # args: value, where it came from
  if ! _valid_version "$1"; then
    red "  ✗ Refusing to act on a version that is not a version (from $2)."
    _write_status "failed" "refused: invalid version string from $2"
    rm -f "$UPDATE_REQUEST_FILE"
    exit 1
  fi
}

# Migration files this database has not recorded. 0 when the database cannot
# be asked — an unreachable database is not evidence of anything pending.
_applied_migrations() {
  if [ "$DB_FAMILY" = mysql ]; then
    velaris_mysql -N -B -e "SELECT filename FROM schema_migrations;" </dev/null 2>/dev/null
  else
    velaris_psql -tAq -c "SELECT filename FROM schema_migrations;" </dev/null 2>/dev/null
  fi
}
_pending_migrations() {
  local applied f n=0
  applied=$(_applied_migrations) || { echo 0; return 0; }
  for f in "$VELARIS_DIR"/migrations/"$DB_FAMILY"/*.sql; do
    [ -f "$f" ] || continue
    grep -qxF "$(basename "$f")" <<<"$applied" || n=$((n + 1))
  done
  echo "$n"
}

# ── Maintenance window check (HH:MM-HH:MM, wrap-around supported) ─
_in_window() {
  local win="$1"
  [[ -z "$win" ]] && return 0
  local start="${win%-*}" end="${win#*-}" now
  now=$(date +%H:%M)
  if [[ "$start" < "$end" ]]; then
    [[ "$now" > "$start" && "$now" < "$end" ]]
  else
    [[ "$now" > "$start" || "$now" < "$end" ]]
  fi
}

AUTO_YES=false
AUTO_MODE=false
for arg in "$@"; do
  case "$arg" in
    --yes)  AUTO_YES=true ;;
    --auto) AUTO_MODE=true; AUTO_YES=true ;;
    --install-timer)
      # Install a systemd timer that polls hourly; the script itself enforces
      # the maintenance window and exits fast when already up to date.
      sudo tee /etc/systemd/system/velaris-update.service > /dev/null <<EOF
[Unit]
Description=Velaris platform auto-update (channel: follows velaris.yaml)
After=network-online.target docker.service

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$VELARIS_DIR
# The PATH of the shell that installed the timer, so the agent finds the same
# uv / npm / docker the operator does.
Environment="PATH=$PATH"
ExecStart=$VELARIS_DIR/update-velaris.sh --auto
EOF
      sudo tee /etc/systemd/system/velaris-update.timer > /dev/null <<EOF
[Unit]
Description=Hourly Velaris update check

[Timer]
OnCalendar=hourly
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
EOF
      # Act on an approval when it is made, not up to an hour later: the Studio
      # button writes .update-request, and this starts the same service then.
      # PathChanged, not PathExists — a request left waiting for its window must
      # not start the service again and again; the hourly timer picks that up.
      sudo tee /etc/systemd/system/velaris-update.path > /dev/null <<EOF
[Unit]
Description=Velaris update — act on an approval made in Studio

[Path]
PathChanged=$UPDATE_REQUEST_FILE
Unit=velaris-update.service

[Install]
WantedBy=paths.target
EOF
      sudo systemctl daemon-reload
      sudo systemctl enable --now velaris-update.timer
      sudo systemctl enable --now velaris-update.path \
        || yellow "  ⚠ velaris-update.path not started — an approval is applied at the hourly check instead"
      green "✓ velaris-update.timer installed and started (hourly check; window: ${UPDATE_WINDOW:-anytime})"
      echo "  An update approved in Studio starts within seconds; one waiting for its window starts at the hourly check."
      exit 0
      ;;
  esac
done

# One updater at a time. With the agent installed, an hourly check or a Studio
# approval can arrive while somebody runs this by hand; two runs pulling code and
# applying migrations side by side is not a state anyone can reason about.
# The lock is on the install directory itself (no lock file to leave behind or
# to ignore in git) and held on a file descriptor, so it is released when this
# process ends, however it ends. Anything this script starts that outlives it
# is started with `9<&-`, or it would keep the lock for as long as it runs.
if command -v flock >/dev/null 2>&1 && exec 9<"$VELARIS_DIR"; then
  if ! flock -n 9; then
    echo "Another update-velaris.sh is already running on this install — nothing to do."
    exit 0
  fi
fi

# --auto honours the operator's settings: opt-in flag + maintenance window.
# A pending UI-approved request (.update-request) overrides the auto_update
# opt-in — the admin explicitly approved this update in Studio. mode="now"
# also skips the maintenance window; mode="window" waits for it.
if [[ "$AUTO_MODE" == true ]]; then
  if [[ -n "$REQUEST_MODE" ]]; then
    AUTO_YES=true
    if [[ "$REQUEST_MODE" != "now" ]]; then
      if [[ -n "$REQUEST_SCHEDULED_FOR" ]]; then
        # Calendar-scheduled slot wins over the static update_window
        NOW_EPOCH=$(date +%s)
        SLOT_EPOCH=$(date -d "$REQUEST_SCHEDULED_FOR" +%s 2>/dev/null || echo 0)
        if [[ "$SLOT_EPOCH" -gt 0 && "$NOW_EPOCH" -lt "$SLOT_EPOCH" ]]; then
          echo "UI update request pending — scheduled after business hours ($REQUEST_SCHEDULED_FOR)"
          exit 0
        fi
      elif ! _in_window "$UPDATE_WINDOW"; then
        echo "UI update request pending — waiting for maintenance window ($UPDATE_WINDOW)"
        exit 0
      fi
    fi
    echo "UI-approved update request found (mode: $REQUEST_MODE) — proceeding"
  else
    if [[ "$AUTO_UPDATE" != "true" ]]; then
      echo "auto_update is disabled in velaris.yaml — nothing to do"
      exit 0
    fi
    if ! _in_window "$UPDATE_WINDOW"; then
      echo "outside maintenance window ($UPDATE_WINDOW) — skipping"
      exit 0
    fi
  fi
fi

echo "╔══════════════════════════════════════════╗"
echo "║         VELARIS Updater                  ║"
echo "╚══════════════════════════════════════════╝"

# ── Step 1: Read current version ─────────────────────────────────
step "Step 1/7 — Reading current version..."
CURRENT_VERSION=$(grep 'version:' "$VELARIS_DIR/velaris.yaml" | head -1 | sed 's/.*version: *"\([^"]*\)".*/\1/')
echo "  Installed: v$CURRENT_VERSION"

# ── Step 2: Fetch target version ─────────────────────────────────
step "Step 2/7 — Checking for updates ($UPDATE_SOURCE, channel: $UPDATE_CHANNEL)..."

# Channel manifest: channels.json maps each channel to the version it should
# run. Promotion = the Velaris team moves a pointer; every env converges on
# its own schedule. Falls back to "latest release" when the manifest is
# unreachable (manual mode only — --auto never guesses).
#
# PUO Phase 4: when deploy/release-signing.pub exists, the manifest MUST
# carry a valid cosign signature (channels.json.sig) — fail closed on any
# verification problem. Without the key file, verification is skipped
# (not configured).
# A private file, not a fixed name in /tmp: any local user could pre-create or
# swap a predictable path between the download and the read, and so choose the
# version this script installs. Removed by _on_exit.
MANIFEST_FILE="$(mktemp "${TMPDIR:-/tmp}/velaris-channels.XXXXXX")"

_manifest_url() {
  if [[ "$UPDATE_SOURCE" == "github" ]]; then
    echo "https://raw.githubusercontent.com/${GITHUB_REPO}/${MANIFEST_BRANCH}/channels.json"
  else
    echo "${UPDATE_SERVER_URL%/latest}/channels.json"
  fi
}

fetch_manifest() {
  # returns: 0 = ok, 1 = unreachable, 2 = signature verification failure
  local url; url=$(_manifest_url)
  curl -sf --max-time 10 "$url" -o "$MANIFEST_FILE" 2>/dev/null || return 1

  local pubkey="$VELARIS_DIR/deploy/release-signing.pub"
  if [[ -f "$pubkey" ]]; then
    if ! command -v cosign >/dev/null 2>&1; then
      red "  ✗ release-signing.pub configured but cosign is not installed — refusing unverified manifest"
      return 2
    fi
    if ! curl -sf --max-time 10 "${url}.sig" -o "${MANIFEST_FILE}.sig" 2>/dev/null; then
      red "  ✗ channels.json.sig missing — refusing unverified manifest"
      return 2
    fi
    if ! cosign verify-blob --key "$pubkey" --signature "${MANIFEST_FILE}.sig" "$MANIFEST_FILE" >/dev/null 2>&1; then
      red "  ✗ manifest signature verification FAILED — possible tampering"
      return 2
    fi
    green "  ✓ manifest signature verified (cosign)"
  fi
  return 0
}

fetch_latest_version() {
  if [[ "$UPDATE_SOURCE" == "github" ]]; then
    # GitHub Releases API — returns tag_name like "v2.1.0"
    local response
    response=$(curl -sf --max-time 10 \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null) || {
      yellow "  ⚠ Could not reach GitHub — skipping update check" >&2
      echo "$CURRENT_VERSION"
      return
    }
    echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))"

  elif [[ "$UPDATE_SOURCE" == "server" ]]; then
    # Own server — expects JSON: { "version": "2.1.0", "notes": "..." }
    local response
    response=$(curl -sf --max-time 10 "$UPDATE_SERVER_URL" 2>/dev/null) || {
      yellow "  ⚠ Could not reach update server — skipping update check" >&2
      echo "$CURRENT_VERSION"
      return
    }
    echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])"
  fi
}

RESUMING=false
RESUME_FROM=""
MANIFEST_RC=0
fetch_manifest || MANIFEST_RC=$?
if [[ "$MANIFEST_RC" -eq 2 ]]; then
  _write_status "manifest-unverified" "channel manifest failed signature verification"
  exit 1
fi

if [[ "$REQUEST_ACTION" == "rollback" ]]; then
  # ── PUO Phase 4: admin-approved rollback (code + images only) ────
  if [[ -z "$REQUEST_TO_VERSION" ]]; then
    red "  ✗ rollback request without to_version — ignoring"
    _write_status "failed" "rollback request missing to_version"
    rm -f "$UPDATE_REQUEST_FILE"
    exit 1
  fi
  _require_valid_version "$REQUEST_TO_VERSION" "the rollback request"
  LATEST_VERSION="$REQUEST_TO_VERSION"
  yellow "  Rollback requested → v$LATEST_VERSION"
  yellow "  Code and images are reverted; DB migrations are NOT reverted"
  yellow "  (expand/contract migrations make old code run on new schema)."
else
  LATEST_VERSION=""
  if [[ "$MANIFEST_RC" -eq 0 ]]; then
    LATEST_VERSION=$(python3 -c "import json; print(json.load(open('$MANIFEST_FILE')).get('$UPDATE_CHANNEL','') or '')" 2>/dev/null || echo "")
  fi

  if [[ -n "$LATEST_VERSION" ]]; then
    echo "  Channel '$UPDATE_CHANNEL' pins: v$LATEST_VERSION"
  else
    if [[ "$AUTO_MODE" == true ]]; then
      yellow "  ⚠ Channel manifest unreachable — auto mode never guesses; skipping"
      _write_status "skipped" "channel manifest unreachable"
      exit 0
    fi
    yellow "  ⚠ Channel manifest unreachable — falling back to latest release"
    LATEST_VERSION=$(fetch_latest_version)
  fi
  _require_valid_version "$LATEST_VERSION" "the channel manifest"

  if [[ "$LATEST_VERSION" == "$CURRENT_VERSION" ]]; then
    # "Already on it" is only true if the last update FINISHED. One that stopped
    # after the code pull leaves velaris.yaml on the new version with its
    # migrations unapplied, and reporting "current" here would skip them for
    # good — new code on the old schema, permanently.
    _PENDING=$(_pending_migrations)
    if [[ -f "$UPDATE_MARKER_FILE" || "$_PENDING" -gt 0 ]]; then
      if [[ "$AUTO_MODE" == true && -z "$REQUEST_MODE" ]]; then
        # The timer on its own never retries a stopped update (the same rule as
        # a failed migration). The beacon keeps the original failure.
        yellow "  ⚠ v$CURRENT_VERSION is checked out but its update did not finish ($_PENDING migration(s) pending)."
        yellow "    Run ./update-velaris.sh by hand, or approve the update in Studio, to finish it."
        exit 0
      fi
      RESUMING=true
      RESUME_FROM=$(head -1 "$UPDATE_MARKER_FILE" 2>/dev/null | tr -cd '0-9A-Za-z.-' || true)
      yellow "  ⚠ v$CURRENT_VERSION is checked out but its update did not finish ($_PENDING migration(s) pending) — finishing it"
    else
      green "  ✓ Already on the channel version (v$CURRENT_VERSION)"
      _write_status "current" "already on v$CURRENT_VERSION"
      rm -f "$UPDATE_REQUEST_FILE"   # request satisfied — nothing newer pinned
      exit 0
    fi
  fi

  # Never downgrade automatically — a manifest behind the installed version
  # means this env ran ahead (e.g. dev box switched to prod channel).
  #
  # But "ahead" is not the same as "finished". An update stopped after its code
  # pull leaves velaris.yaml on the new version with migrations unapplied; the
  # check above catches that only when the channel pins exactly that version.
  # Pinned anything older and this branch said "not downgrading", exit 0, and
  # left new code on the old schema for the next restart (R3 2026-11-01). Finish
  # the installed version first — the same rules as the branch above.
  if [[ "$RESUMING" == false && "$LATEST_VERSION" != "$CURRENT_VERSION" ]] \
     && [[ "$(printf '%s\n%s\n' "$LATEST_VERSION" "$CURRENT_VERSION" | sort -V | tail -1)" == "$CURRENT_VERSION" ]]; then
    _PENDING=$(_pending_migrations)
    if [[ -f "$UPDATE_MARKER_FILE" || "$_PENDING" -gt 0 ]]; then
      if [[ "$AUTO_MODE" == true && -z "$REQUEST_MODE" ]]; then
        yellow "  ⚠ v$CURRENT_VERSION is checked out but its update did not finish ($_PENDING migration(s) pending)."
        yellow "    Run ./update-velaris.sh by hand, or approve the update in Studio, to finish it."
        exit 0
      fi
      RESUMING=true
      RESUME_FROM=$(head -1 "$UPDATE_MARKER_FILE" 2>/dev/null | tr -cd '0-9A-Za-z.-' || true)
      yellow "  ⚠ v$CURRENT_VERSION is checked out but its update did not finish ($_PENDING migration(s) pending) — finishing it"
      yellow "    (channel '$UPDATE_CHANNEL' pins v$LATEST_VERSION; that is not a reason to leave this one half-done)"
      LATEST_VERSION="$CURRENT_VERSION"
    fi
  fi
  if [[ "$RESUMING" == false ]] && [[ "$(printf '%s\n%s\n' "$LATEST_VERSION" "$CURRENT_VERSION" | sort -V | tail -1)" == "$CURRENT_VERSION" ]]; then
    yellow "  ⚠ Channel pins v$LATEST_VERSION but v$CURRENT_VERSION is installed — not downgrading"
    _write_status "skipped" "channel v$LATEST_VERSION behind installed v$CURRENT_VERSION"
    rm -f "$UPDATE_REQUEST_FILE"   # unsatisfiable request
    exit 0
  fi

  # PUO Phase 4: min_upgrade_from — never skip required migration steps
  # (e.g. 1.0 must go through 1.5 before 2.0).
  if [[ "$MANIFEST_RC" -eq 0 ]]; then
    MIN_FROM=$(python3 -c "import json; r=json.load(open('$MANIFEST_FILE')).get('releases',{}).get('$LATEST_VERSION',{}) or {}; print(r.get('min_upgrade_from') or '')" 2>/dev/null || echo "")
    if [[ -n "$MIN_FROM" ]] \
       && [[ "$(printf '%s\n%s\n' "$MIN_FROM" "$CURRENT_VERSION" | sort -V | head -1)" == "$CURRENT_VERSION" ]] \
       && [[ "$CURRENT_VERSION" != "$MIN_FROM" ]]; then
      red "  ✗ v$LATEST_VERSION requires at least v$MIN_FROM (installed: v$CURRENT_VERSION)"
      red "    Upgrade to v$MIN_FROM first — version path enforcement."
      _write_status "blocked" "v$LATEST_VERSION requires upgrading via v$MIN_FROM first"
      rm -f "$UPDATE_REQUEST_FILE"
      exit 1
    fi
  fi
fi

echo ""
if [[ "$RESUMING" == true ]]; then
  green "  Finishing the update to v$LATEST_VERSION"
else
  green "  New version available: v$CURRENT_VERSION → v$LATEST_VERSION"
fi
echo ""

# ── Confirm ───────────────────────────────────────────────────────
if [[ "$AUTO_YES" == false ]]; then
  read -rp "  Proceed with update? [y/N]: " CONFIRM
  [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "  Aborted."; exit 0; }
fi

# ── Step 3: Backup database ───────────────────────────────────────
_IN_UPDATE=true   # from here a stop is reported by _on_exit
step "Step 3/7 — Backing up database..."
mkdir -p "$BACKUP_DIR"
# Owner-only: this dump is every tenant's data in plain SQL (2026-10-16 — it was
# written with the default umask, i.e. group/world-readable).
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
BACKUP_FILE="$BACKUP_DIR/velaris-v${CURRENT_VERSION}-$(date +%Y%m%d-%H%M%S).sql"
( umask 077; : > "$BACKUP_FILE" )

if [ "$DB_FAMILY" = mysql ]; then _db_dump() { velaris_mysqldump; }; else _db_dump() { velaris_pg_dump; }; fi
if _db_dump > "$BACKUP_FILE" 2>"$VELARIS_SCRATCH/backup-err.log" && [ -s "$BACKUP_FILE" ]; then
  green "  ✓ Backup saved: $BACKUP_FILE"
else
  red "  ✗ DB backup failed — aborting to protect your data"
  tail -3 "$VELARIS_SCRATCH/backup-err.log" 2>/dev/null | sed 's/^/    /'
  if [ "$DB_FAMILY" = mysql ]; then red "    Check the ${_DB_BACKEND} server named in velaris.yaml (db_host) and VELARIS_DB_PASSWORD"
  else red "    Check: docker logs $DB_CONTAINER"; fi
  exit 1
fi

# ── Record the backup in HxDR (§8.9) ──────────────────────────────
# This script is the one place that reliably knows a good backup exists, and
# until it said so `GET /dr/posture` reported `no_backup` for every region
# while backups were in fact being taken.
#
# Runs BEFORE the code pull in Step 4 deliberately — the old tree is still
# intact here, so the CLI is guaranteed importable.
#
# Never fatal, and `set -e` is suspended for it: a bookkeeping row must not
# turn a successful backup into a failed update. The CLI exits 0 on its own
# failures too; `|| true` covers the interpreter not being there at all.
#
# Runs from $VELARIS_DIR, not from services/case-service: `env_file: ".env"` is
# resolved against the working directory, and the .env lives at the root. This
# mirrors how start-velaris.sh launches the service (root + --app-dir).
if [[ -x "$VELARIS_DIR/.venv/bin/python" ]]; then
  set +e
  (cd "$VELARIS_DIR" && \
   PYTHONPATH="$VELARIS_DIR/services/case-service" \
   "$VELARIS_DIR/.venv/bin/python" -m case_service.compliance.dr_cli \
     --location "$BACKUP_FILE" \
     --scope database \
     --status succeeded \
     --recorded-by "update-velaris.sh v${CURRENT_VERSION}") || true
  set -e
else
  yellow "  ! DR: .venv not found — backup taken but not recorded in /dr/posture"
fi

# ── Step 4: Pull latest code ──────────────────────────────────────
step "Step 4/7 — Pulling latest code..."
cd "$VELARIS_DIR"
# Written before the tree changes and removed only when the run reaches its end:
# while it exists, "already on the channel version" is not to be believed.
[[ -f "$UPDATE_MARKER_FILE" ]] || echo "$CURRENT_VERSION" > "$UPDATE_MARKER_FILE"

# Protect per-host OpenBao runtime secrets (unseal keys, root token, AppRole)
# from the update. They live in the tree but are generated per-server; while any
# stay git-tracked, a checkout/pull would abort, and a tarball extract would
# overwrite them. Strategy: back up → clear local mods so git won't refuse →
# update → restore the host's live copies → verify → drop the backup.
_SECRET_FILES=(
  "deploy/openbao/.bao-init.json"
  "deploy/openbao/agent/role_id"
  "deploy/openbao/agent/secret_id"
)
_SECRET_BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/velaris-secrets.XXXXXX")"
for _f in "${_SECRET_FILES[@]}"; do
  if [ -f "$_f" ]; then
    mkdir -p "$_SECRET_BACKUP/$(dirname "$_f")"
    cp -p "$_f" "$_SECRET_BACKUP/$_f"
    if git ls-files --error-unmatch "$_f" >/dev/null 2>&1; then
      git checkout -- "$_f" 2>/dev/null || true   # tracked + modified → drop local edit
    fi
  fi
done

if [[ "$UPDATE_SOURCE" == "github" ]]; then
  git fetch origin --tags
  git checkout "v${LATEST_VERSION}" 2>/dev/null || git checkout "tags/v${LATEST_VERSION}" 2>/dev/null || {
    # If no tag, just pull latest main
    yellow "  ⚠ No tag v${LATEST_VERSION} found — pulling latest main"
    git pull origin main
  }
elif [[ "$UPDATE_SOURCE" == "server" ]]; then
  # Own server: download and extract tarball
  TARBALL_URL="${UPDATE_SERVER_URL%/latest}/download/v${LATEST_VERSION}.tar.gz"
  curl -sf --max-time 120 -o "$VELARIS_SCRATCH/velaris-update.tar.gz" "$TARBALL_URL"
  # velaris.yaml holds the operator's own settings (update source, server, channel,
  # schedule, database) beside the shipped ones; the tarball's copy would reset
  # them all to the defaults (R3 2026-11-01: an install on its own update server
  # was switched back to GitHub). Keep it aside and carry its values over below.
  _YAML_KEPT="$VELARIS_SCRATCH/velaris.yaml.before-update"
  cp -p "$VELARIS_DIR/velaris.yaml" "$_YAML_KEPT" 2>/dev/null || _YAML_KEPT=""
  _NEW_FILES="$VELARIS_SCRATCH/velaris-release-files.new"
  tar -tzf "$VELARIS_SCRATCH/velaris-update.tar.gz" | cut -d/ -f2- | grep -v '^$' | grep -v '/$' | LC_ALL=C sort -u > "$_NEW_FILES"
  # The installed release's own list — read BEFORE the extract, which brings
  # the new release's list in under the same name.
  _OLD_FILES="$VELARIS_SCRATCH/velaris-release-files.old"
  cp "$VELARIS_DIR/.release-files" "$_OLD_FILES" 2>/dev/null || : > "$_OLD_FILES"
  # Run as root, tar gives every file the PACKAGER's user id and rewrites each
  # existing folder's owner and mode from the archive (R6 2026-11-06: a 3.1.17
  # update made deploy/docker-compose/pooler-socket another user's 0700 folder,
  # the services lost the database pooler, and the whole platform stopped).
  # So: existing folders keep their owner and mode (--no-overwrite-dir), nothing
  # takes an owner from the archive (--no-same-owner), and what this release
  # shipped belongs to the install's owner (the owner of the install folder) —
  # the user a git install's files already belong to.
  tar -xzf "$VELARIS_SCRATCH/velaris-update.tar.gz" -C "$VELARIS_DIR" --strip-components=1 \
      --no-same-owner --no-overwrite-dir
  rm -f "$VELARIS_SCRATCH/velaris-update.tar.gz"
  _VD_OWNER="$(stat -c '%u:%g' "$VELARIS_DIR" 2>/dev/null || echo 0:0)"
  if [[ "$(id -u)" == 0 && "${_VD_OWNER%%:*}" != 0 ]]; then
    ( cd "$VELARIS_DIR" || exit 0
      _VD_REAL="$(pwd -P)"
      # Root changing owners inside a folder the install owner can write: a
      # folder on the way swapped for a symlink would carry the chown outside
      # the install (/etc/...). Only paths whose real parent is inside the
      # install are touched, and never through a link (chown -h), as the
      # removal of old files below.
      _inside() { local _p; _p="$(cd "$1" 2>/dev/null && pwd -P)" || return 1
                  case "$_p" in "$_VD_REAL"|"$_VD_REAL"/*) return 0 ;; *) return 1 ;; esac; }
      # The shipped files (never a path that leaves the install) ...
      grep -v -e '^/' -e '\.\.' "$_NEW_FILES" | while IFS= read -r _f; do
        if [[ -e "$_f" || -L "$_f" ]] && _inside "$(dirname "$_f")"; then printf '%s\0' "$_f"; fi
      done | xargs -0 -r chown -h "$_VD_OWNER"
      # ... and the folders this extract created (root's); an existing folder
      # is left exactly as it was.
      grep -v -e '^/' -e '\.\.' "$_NEW_FILES" | sed -n 's|/[^/]*$||p' \
        | awk -F/ '{p=""; for (i=1; i<=NF; i++) { p = (i>1 ? p "/" : "") $i; print p }}' \
        | LC_ALL=C sort -u | while IFS= read -r _d; do
            if [[ -d "$_d" && ! -L "$_d" && "$(stat -c %u "$_d")" == 0 ]] && _inside "$_d"; then printf '%s\0' "$_d"; fi
          done | xargs -0 -r chown -h "$_VD_OWNER"
    ) || yellow "  ⚠ could not give the new files to the install owner ($_VD_OWNER)"
  fi
  # A tarball only adds: files the installed release shipped and this one does
  # not were left behind — on a rollback they broke `uv sync` (a 3.1.3-only
  # service in 3.1.2's workspace) and the Studio build. Remove exactly those:
  # the previous release's own file list minus this one's. Nothing an install
  # generated or an operator added is in the previous list, so nothing of theirs
  # can go. No list yet (the first tarball update) = nothing removed, as before.
  if [[ -s "$_OLD_FILES" ]]; then
    _gone=0
    _VD_REAL="$(cd "$VELARIS_DIR" && pwd -P)"
    while IFS= read -r _f; do
      case "$_f" in ""|/*|*..*) continue ;; esac
      # Never what an install generates or holds, whatever a list says — the
      # same set the release twin and packages exclude (velaris-mirror-exclude).
      case "$_f" in
        .env|.env.*|velaris-install-report.md|.release-files|.update-*|\
        .backups/*|.standby/*|data/*|logs/*|deploy/tls/certs/*|deploy/openbao/.bao-init.json|\
        deploy/openbao/enabled|deploy/openbao/agent/role_id|deploy/openbao/agent/secret_id|\
        deploy/openbao/agent/out/*|services/case-service/data/*) continue ;;
      esac
      # And never outside the install: a directory on the way that is a
      # symlink elsewhere would carry the removal with it.
      _parent="$(cd "$VELARIS_DIR/$(dirname "$_f")" 2>/dev/null && pwd -P)" || continue
      case "$_parent" in "$_VD_REAL"|"$_VD_REAL"/*) ;; *) continue ;; esac
      if [[ -f "$VELARIS_DIR/$_f" || -L "$VELARIS_DIR/$_f" ]]; then
        rm -f "$VELARIS_DIR/$_f" && _gone=$((_gone + 1))
      fi
    done < <(LC_ALL=C comm -23 <(LC_ALL=C sort -u "$_OLD_FILES") "$_NEW_FILES")
    # Directories the removal emptied, and their emptied parents; a directory
    # with anything left in it stays.
    LC_ALL=C comm -23 <(LC_ALL=C sort -u "$_OLD_FILES") "$_NEW_FILES" | sed -n 's|/[^/]*$||p' | LC_ALL=C sort -ru \
      | (cd "$VELARIS_DIR" && xargs -r -d '\n' rmdir -p --ignore-fail-on-non-empty 2>/dev/null) || true
    if [[ "$_gone" -gt 0 ]]; then
      green "  ✓ removed $_gone file(s) the previous release shipped and v${LATEST_VERSION} does not"
    fi
  fi
  cp "$_NEW_FILES" "$VELARIS_DIR/.release-files"
  # Carry the operator's own settings over, and only those — the keys setup and
  # the operator write (instance name, database + connection components, the
  # whole updates block). Every other key keeps the release's value: a shipped
  # default the release changed on purpose (events: redpanda → kafka) must not
  # be pinned to the old one. Comments stay. Inline, not a script in the tree:
  # on a rollback the tree is the OLD release, which may not have one.
  if [[ -n "$_YAML_KEPT" && -f "$_YAML_KEPT" ]]; then
    python3 - "$_YAML_KEPT" "$VELARIS_DIR/velaris.yaml" <<'PY' \
      || { yellow "  ⚠ could not carry velaris.yaml settings over — restored the previous file"; cp -p "$_YAML_KEPT" "$VELARIS_DIR/velaris.yaml"; }
import re, sys
LINE = re.compile(r'^(\s*)([A-Za-z0-9_.-]+):(\s*)(.*?)(\s+#.*)?$')
OWN = ("velaris.instance", "velaris.database")
OWN_PREFIX = ("velaris.updates.",)
DB_PARTS = ("db_host", "db_port", "db_name", "db_user")      # written by setup under `database:`
def walk(lines):
    out, stack = [], []
    for ln in lines:
        m = LINE.match(ln.rstrip("\n"))
        if not m or ln.lstrip().startswith(("#", "- ")):
            out.append(None); continue
        ind = len(m.group(1))
        while stack and stack[-1][0] >= ind:
            stack.pop()
        stack.append((ind, m.group(2)))
        out.append((".".join(k for _, k in stack), m))
    return out
own = lambda path: path in OWN or path.startswith(OWN_PREFIX)
old = open(sys.argv[1]).read().splitlines(True)
new = open(sys.argv[2]).read().splitlines(True)
old_w = walk(old)
keep = {p[0]: p[1].group(4) for p in old_w if p and p[1].group(4) != ""}
db_lines = [ln if ln.endswith("\n") else ln + "\n" for ln, p in zip(old, old_w)
            if p and p[0].rsplit(".", 1)[-1] in DB_PARTS]
new_has_db = any(p and p[0].rsplit(".", 1)[-1] in DB_PARTS for p in walk(new))
res = []
for ln, p in zip(new, walk(new)):
    if p and p[1].group(4) != "" and own(p[0]) and p[0] in keep:
        m = p[1]
        ln = f"{m.group(1)}{m.group(2)}:{m.group(3) or ' '}{keep[p[0]]}{m.group(5) or ''}\n"
    res.append(ln)
    if p and p[0] == "velaris.database" and db_lines and not new_has_db:
        res.extend(db_lines)
open(sys.argv[2], "w").write("".join(res))
PY
  fi
fi

# Restore the host's live secrets (overwrites whatever the update brought),
# verify each, and only drop the backup on full success.
_secrets_ok=1
for _f in "${_SECRET_FILES[@]}"; do
  if [ -f "$_SECRET_BACKUP/$_f" ]; then
    mkdir -p "$(dirname "$_f")"
    if cp -p "$_SECRET_BACKUP/$_f" "$_f" && cmp -s "$_SECRET_BACKUP/$_f" "$_f"; then :; else
      _secrets_ok=0; red "  ✗ Failed to restore OpenBao secret: $_f"
    fi
  fi
done
if [ "$_secrets_ok" = 1 ]; then
  rm -rf "$_SECRET_BACKUP"
else
  red "  Kept secret backup at: $_SECRET_BACKUP — restore manually before restarting."
fi

green "  ✓ Code updated to v$LATEST_VERSION"

# ── Step 4b: Refresh dependencies ─────────────────────────────────
# New releases can add Python/npm packages (e.g. livekit-client in 2.1.0);
# stale .venv/node_modules break services and Vite until reinstalled.
step "Step 4b — Refreshing dependencies..."
# Re-source the profile rules from the tree Step 4 just pulled: the copy loaded
# at the top of this script is the PREVIOUS release's, so it neither knows a new
# profile (e.g. `events`) nor defines velaris_retire_redpanda (Step 5b), nor
# sets VELARIS_EVENT_BROKER_ACTIVE (below).
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh" ] && . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"
if command -v uv >/dev/null 2>&1; then
  # Optional ML extras are NOT part of the default install (a base box stays lean,
  # no model weights). But `uv sync` is exact by default — it prunes anything not
  # declared, so an operator who installed an extra would silently lose it on the
  # next run. Detect what is already present and ask for it again.
  _EXTRAS=""
  uv run python -c "import faster_whisper" >/dev/null 2>&1 && _EXTRAS="$_EXTRAS --extra intelligence"
  uv run python -c "import av" >/dev/null 2>&1 && _EXTRAS="$_EXTRAS --extra kyc"
  # The MySQL/MariaDB driver (aiomysql + pymysql) is an extra: without it a
  # MySQL/MariaDB install cannot reach its own database, and `uv sync` (exact)
  # would remove one installed by hand. Fixed 2026-10-16 — it was never installed.
  case "${_DB_BACKEND:-postgresql}" in mysql|mariadb) _EXTRAS="$_EXTRAS --extra mysql" ;; esac
  # The event bus client: needed when the bus is on, kept when already present.
  if [ "${VELARIS_EVENT_BROKER_ACTIVE:-false}" = true ] \
     || uv run python -c "import aiokafka" >/dev/null 2>&1; then
    _EXTRAS="$_EXTRAS --extra events"
  fi
  uv sync --all-packages $_EXTRAS 2>&1 | tail -3
  green "  ✓ Python dependencies synced"
else
  yellow "  ⚠ uv not found — skipping Python dependency sync"
fi
if [ -d "$VELARIS_DIR/studio" ]; then
  if ( cd "$VELARIS_DIR/studio" && { npm install --prefer-offline >"$VELARIS_SCRATCH/velaris-npm-install.log" 2>&1 \
         || npm install >"$VELARIS_SCRATCH/velaris-npm-install.log" 2>&1; } \
       && rm -rf node_modules/.vite ); then
    green "  ✓ Studio npm dependencies installed"
  else
    tail -5 "$VELARIS_SCRATCH/velaris-npm-install.log"
    yellow "  ⚠ npm install FAILED — Studio may not load. Full log: $VELARIS_SCRATCH/velaris-npm-install.log"
  fi
fi

# ── Step 5: Pull new Docker images ───────────────────────────────
step "Step 5/7 — Pulling new Docker images..."
# --env-file is REQUIRED: compose interpolates the whole file for every
# subcommand, and two services declare secrets with `${VAR:?...}`. Without it
# this aborts with "required variable VELARIS_MINIO_PASSWORD is missing" on any
# host that lacks a stray .env beside the compose file — i.e. every real server.
#
# A failed pull is NOT a reason to stop. On 2026-10-12 the `minio/mc` repository
# had been withdrawn from Docker Hub; `pull` exited non-zero, `set -e` ended the
# script here without a word, and the install was left with the new code checked
# out and none of its migrations applied. An image this host already holds keeps
# working; one it truly lacks fails loudly at `up`, where it is named.
#
# The bundled MinIO's two services are left out of the pull. Their images are no
# longer on Docker Hub, and one failed image makes Compose interrupt the whole
# pull, so on an install that runs them no other image was ever refreshed. The
# host already holds them (that is what switched the profile on); there is
# nothing newer to fetch. If the service list cannot be read, pull everything,
# as before.
_pull_services=()
while IFS= read -r _svc; do if [ -n "$_svc" ]; then _pull_services+=("$_svc"); fi; done < <(
  docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" config --services 2>/dev/null | grep -vxE 'minio|minio-init' || true)
if docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" pull ${_pull_services[@]+"${_pull_services[@]}"}; then
  green "  ✓ Images updated"
else
  yellow "  ⚠ Some images could not be pulled — continuing with the ones already on this host"
fi

# ── Step 5b: Reclaim superseded layers + keep the cache cap in place ──
# Every update supersedes image layers and (where a service builds) leaves the
# previous build cache behind. Docker expires neither on its own, so an install
# that is updated often grows without bound — this is where that is paid back.
step "Step 5b — Reclaiming Docker disk..."
# shellcheck source=scripts/docker-storage.sh
[ -f "$VELARIS_DIR/scripts/docker-storage.sh" ] && . "$VELARIS_DIR/scripts/docker-storage.sh"

if type vds_write_daemon_json >/dev/null 2>&1; then
  # Self-heal: an install predating the cap, or one whose daemon.json was
  # replaced, gets it back here rather than only on a fresh setup.
  if vds_write_daemon_json "" "$(vds_current_root)"; then
    vds_restart_docker && green "  ✓ Build-cache cap applied" \
      || yellow "  ⚠ Docker restart failed — cap applies on the next restart"
  fi
  vds_prune_cache 5GB
else
  # Helper absent (older tree): fall back to the one safe reclaim.
  docker builder prune -f >/dev/null 2>&1 || true
  green "  ✓ Build cache trimmed"
fi

# Redpanda was replaced by Apache Kafka (November 2026): remove a leftover
# broker container and its image; its data volume is kept (compose-profiles.sh,
# re-sourced at Step 5).
type velaris_retire_redpanda >/dev/null 2>&1 && velaris_retire_redpanda
# A pooler turned off since the last start (compose-profiles.sh).
type velaris_retire_pooler >/dev/null 2>&1 && velaris_retire_pooler

# Dangling IMAGES only — layers no tag and no container references. Volumes are
# deliberately NOT pruned: an unreferenced volume here can still be real data.
docker image prune -f >/dev/null 2>&1 || true
green "  ✓ Superseded image layers removed"

# ── Step 6: Run migrations ────────────────────────────────────────
if [[ "$REQUEST_ACTION" == "rollback" ]]; then
  step "Step 6/7 — Skipping migrations (rollback: schema stays, old code runs on it)"
else
step "Step 6/7 — Running database migrations..."

if [ "$DB_FAMILY" = mysql ]; then
# MySQL / MariaDB: a server the operator runs — never started here, only waited
# for. Same DDL and loop as setup-velaris.sh's MySQL path, one track for both.
for i in $(seq 1 20); do
  velaris_mysql -e "SELECT 1" </dev/null >/dev/null 2>&1 && break
  [ "$i" -eq 20 ] && { red "  ✗ ${_DB_BACKEND} not reachable at $(_vx_yaml db_host 127.0.0.1):$(_vx_yaml db_port 3306)"; exit 1; }
  sleep 1
done
velaris_mysql -e "CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   VARCHAR(255) PRIMARY KEY,
  applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
);" </dev/null
APPLIED=0; SKIPPED=0
_APPLIED_MIGRATIONS="$(_applied_migrations || true)"
for migration_file in $(find "$VELARIS_DIR/migrations/mysql" -maxdepth 1 -name "*.sql" | sort); do
  filename="$(basename "$migration_file")"
  if grep -qxF "$filename" <<<"$_APPLIED_MIGRATIONS"; then
    SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  # The mysql client stops at the first failing statement and exits non-zero,
  # so a failure is never recorded as applied.
  if velaris_mysql < "$migration_file" 2>"$VELARIS_SCRATCH/migration_err.log"; then
    velaris_mysql -e "INSERT IGNORE INTO schema_migrations(filename) VALUES('$filename');" </dev/null
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"
    grep -v "Using a password" "$VELARIS_SCRATCH/migration_err.log"
    red "  Migration failed — rolling back to v$CURRENT_VERSION"
    red "  Restore backup with: mysql -h <db_host> -u <db_user> -p <db_name> < $BACKUP_FILE"
    _write_status "failed" "migration $filename failed during update to v$LATEST_VERSION"
    rm -f "$UPDATE_REQUEST_FILE"
    exit 1
  fi
done
else

# Ensure DB is up before migrating (--env-file required — see Step 5). A
# database on another server is not ours to start; only waited for.
velaris_is_external database \
  || docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d velaris-db
for i in $(seq 1 20); do
  velaris_pg_isready 2>/dev/null && break
  [ "$i" -eq 20 ] && { red "  ✗ PostgreSQL not ready"; exit 1; }
  sleep 1
done
# Query statistics: an update never turns them on (VELARIS_DB_QUERY_STATS unset =
# untouched); it applies what the operator set, before the services reconnect.
"$VELARIS_DIR/scripts/db-query-stats.sh" || yellow "  ⚠ query statistics not applied — ./scripts/db-query-stats.sh"

velaris_psql -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

APPLIED=0; SKIPPED=0

# What is already applied, read ONCE: one query, not one per file — against a
# database on another server each query is a client container (~1 s).
_APPLIED_MIGRATIONS="$(velaris_psql -tAq -c "SELECT filename FROM schema_migrations;" </dev/null 2>/dev/null || true)"
for migration_file in $(find "$VELARIS_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | sort); do
  filename="$(basename "$migration_file")"
  EXISTS=""; grep -qxF "$filename" <<<"$_APPLIED_MIGRATIONS" && EXISTS=1
  if [[ "$EXISTS" == "1" ]]; then
    SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  # ON_ERROR_STOP is what makes a failure a failure — the same line
  # setup-velaris.sh carries, and for the same reason. Without it psql runs the
  # remaining statements after a rejected one, exits 0, and the file is recorded
  # in schema_migrations as applied: a half-applied migration that never runs
  # again, on an EXISTING install, unattended. `setup` learned this from
  # migration 080; the update path never had the fix.
  if velaris_psql -q \
      -v ON_ERROR_STOP=1 < "$migration_file" 2>"$VELARIS_SCRATCH/migration_err.log"; then
    velaris_psql -q \
      -c "INSERT INTO schema_migrations(filename) VALUES('$filename') ON CONFLICT DO NOTHING;" </dev/null
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"
    cat "$VELARIS_SCRATCH/migration_err.log"
    red "  Migration failed — rolling back to v$CURRENT_VERSION"
    red "  Restore backup with: psql -U $DB_USER $DB_NAME < $BACKUP_FILE"
    _write_status "failed" "migration $filename failed during update to v$LATEST_VERSION"
    rm -f "$UPDATE_REQUEST_FILE"   # consumed — never auto-retry a failing update
    exit 1
  fi
done
fi
green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"
fi

# ── Step 6b: Sync .env with new variables from .env.example ──────
# New releases may introduce new configuration variables. The user's .env
# is never overwritten — variables it already has (active OR commented)
# are left untouched; only genuinely new ones are appended verbatim:
#   - active lines from .env.example arrive active (shipped default)
#   - commented lines arrive commented (documentation of the new knob)
step "Step 6b — Syncing .env with new variables..."

ENV_FILE="$VELARIS_DIR/.env"
EXAMPLE_FILE="$VELARIS_DIR/.env.example"
ENV_ADDED=0
ENV_ADDED_KEYS=" "

if [[ -f "$ENV_FILE" && -f "$EXAMPLE_FILE" ]]; then
  while IFS= read -r line; do
    key=""
    if [[ "$line" =~ ^([A-Z][A-Z0-9_]+)= ]]; then
      key="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^#[[:space:]]([A-Z][A-Z0-9_]+)= ]]; then
      key="${BASH_REMATCH[1]}"
    fi
    [[ -z "$key" ]] && continue
    grep -qE "^#?[[:space:]]*${key}=" "$ENV_FILE" && continue

    if [[ "$ENV_ADDED" -eq 0 ]]; then
      {
        echo ""
        echo "# ── Added by update-velaris.sh → v${LATEST_VERSION} ($(date +%Y-%m-%d)) ──"
      } >> "$ENV_FILE"
    fi
    echo "$line" >> "$ENV_FILE"
    echo "    + $key"
    ENV_ADDED=$((ENV_ADDED + 1))
    ENV_ADDED_KEYS="${ENV_ADDED_KEYS}${key} "
  done < "$EXAMPLE_FILE"

  # Newly appended secrets ship empty — generate real values for them. ONLY for
  # a key this run appended: an empty value the install already had is the
  # operator's (or an older setup's) setting, and filling it in is turning
  # something on. R3 2026-11-01: an install with no storage master key had
  # encryption at rest switched on by an unattended update, the key only in .env.
  _gen_if_empty() {
    local key="$1" val="$2"
    [[ "$ENV_ADDED_KEYS" == *" ${key} "* ]] || return 0
    if grep -q "^${key}=$" "$ENV_FILE"; then
      sed -i "s|^${key}=$|${key}=${val}|" "$ENV_FILE"
      echo "    generated ${key}"
    fi
  }
  _gen_if_empty VELARIS_CASE_AUTH_SECRET        "$(openssl rand -hex 32)"
  _gen_if_empty VELARIS_CASE_STORAGE_MASTER_KEY "$(openssl rand -hex 32)"
  _gen_if_empty VELARIS_DB_PASSWORD           "$(openssl rand -hex 16)"
  _gen_if_empty VELARIS_ADMIN_PASSWORD        "$(openssl rand -hex 16)"
  _gen_if_empty VELARIS_SEARCH_PASSWORD       "$(openssl rand -hex 16)"
  # VELARIS_GATEWAY_IDENTITY_SECRET is deliberately NOT generated here.
  #
  # Generating it would switch workload identity (#18) ON during an unattended
  # update — the timer runs this on its own — and enforcement that arrives
  # without anyone watching is enforcement nobody can diagnose at 3am. An
  # existing install therefore keeps the check off until an operator turns it on
  # deliberately (deploy/tls/README.md); `setup-velaris.sh` generates one for
  # NEW installs, where there is nothing to lock out.
  #
  # This is a decision, not an oversight — do not "fix" it by adding a line here.

  # Backup key: an install from before November 2026 has none. Generated here
  # (user's decision, 2026-10-16 — every install gets one; pre-customer
  # exception in CLAUDE.md). The private key goes ONLY into
  # velaris-install-report.md (0600) — see that file. Never fatal.
  if [[ -x "$VELARIS_DIR/.venv/bin/python" && -f "$VELARIS_DIR/scripts/backup/velaris_backup.py" ]]; then
    "$VELARIS_DIR/.venv/bin/python" "$VELARIS_DIR/scripts/backup/velaris_backup.py" ensure-key \
      || yellow "  ⚠ Backup key not set — run ./backup-velaris.sh --ensure-key"
  fi

  # The internal service secret (R5e step 7): an install from before the commerce
  # split has none, and without it case-service's /internal/v1 routes refuse, so
  # checkout could not open an order's case. Generated here — user's decision,
  # 2026-10-19, pre-customer exception in CLAUDE.md — into .env and, where OpenBao
  # holds the secrets, into OpenBao (compose-profiles.sh). Never printed.
  type velaris_ensure_internal_secret >/dev/null 2>&1 && velaris_ensure_internal_secret
  # The gateway's optional settings, as include files (never template variables).
  type velaris_gateway_options >/dev/null 2>&1 && velaris_gateway_options

  if [[ "$ENV_ADDED" -gt 0 ]]; then
    green "  ✓ Added $ENV_ADDED new variable(s) to .env — review them after the update"
  else
    green "  ✓ .env already has every variable — nothing to sync"
  fi
else
  yellow "  ⚠ .env or .env.example missing — skipping env sync"
fi

# ── Step 7: Update version + restart services ─────────────────────
step "Step 7/7 — Restarting services..."

# Stamp new version into velaris.yaml
sed -i "s/version: \"$CURRENT_VERSION\"/version: \"$LATEST_VERSION\"/" "$VELARIS_DIR/velaris.yaml"

# Restart. Two ways, because an install runs one of two ways.
#
# Under systemd, start-velaris.sh refuses to start a second copy and exits at
# once — before its manifest sync, and without restarting anything. Until
# 2026-10-11 that was the whole of this step there: new code on disk, old
# processes in memory, Studio still serving the previous build, and the health
# gate below passing against the old processes. refresh-after-update.sh does
# what was skipped: manifest, Studio rebuild, restart.
RESTART_PENDING=false
if systemctl is-active --quiet velaris-case-service 2>/dev/null \
   || systemctl is-active --quiet velaris-engine 2>/dev/null; then
  _refresh_args=()
  [[ "$AUTO_MODE" == true ]] && _refresh_args+=(--non-interactive)
  _refresh_rc=0
  DB_CONTAINER="$DB_CONTAINER" DB_USER="$DB_USER" DB_NAME="$DB_NAME" \
    bash "$VELARIS_DIR/deploy/systemd/refresh-after-update.sh" "${_refresh_args[@]}" 9<&- || _refresh_rc=$?
  [[ "$_refresh_rc" -ne 0 ]] && RESTART_PENDING=true
else
  # Restart via the standard startup script (handles all service health checks)
  bash "$VELARIS_DIR/start-velaris.sh" 9<&-
fi

# The proxies read their config from files bind-mounted out of the tree. An
# update replaces those files (a new inode), and `compose up -d` sees no change
# in the service definition, so the running container keeps the OLD file. Found
# 2026-11-01 rolling back 3.1.3 → 3.1.2: the gateway kept 3.1.3's routes, sent
# sign-in to a user-service 3.1.2 does not run, and every login was a 502 while
# /health stayed green. Recreate them from the release now on disk — forward and
# back alike. Only the services this release's compose file defines.
_proxy_svcs=""
for _p in gateway broker-gw egress-gw; do
  docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" config --services 2>/dev/null \
    | grep -qx "$_p" && _proxy_svcs="$_proxy_svcs $_p"
done
if [[ -n "$_proxy_svcs" ]]; then
  # shellcheck disable=SC2086
  if docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" \
       up -d --no-deps --force-recreate $_proxy_svcs >/dev/null 2>&1; then
    green "  ✓ proxies recreated on this release's config:$_proxy_svcs"
  else
    yellow "  ⚠ could not recreate:$_proxy_svcs — run: docker compose -f $COMPOSE_FILE --env-file .env up -d --force-recreate$_proxy_svcs"
  fi
fi

# ── Health gate: never report success on a broken env ─────────────
# Every API service, not just the gateway. Since 13c-e the platform is four
# processes: the gateway answers /health from case-service, so a dead
# analytics-, hxgraph-, hxmigrate- or commerce-service would have left this gate green
# while a quarter of the API returned 502.
# Probe the way the services actually came up. With internal TLS on they demand
# the gateway's client certificate at the handshake, so a plain http probe would
# fail against a perfectly healthy platform — and this gate is what decides
# whether the update is considered successful.
_velaris_probe() { curl -sf --max-time 3 "http://localhost:$1/health" >/dev/null 2>&1; }
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh" ] && . "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh"

# The worker service (R5e step 9) runs where its unit is installed, or where
# start-velaris.sh runs the platform; on a systemd box without its unit the jobs
# still run in case-service, so there is no worker to check.
_WORKER_PROBE=""
if [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-worker-service.service" ] \
   || ! systemctl is-active --quiet velaris-case-service 2>/dev/null; then
  _WORKER_PROBE="${VELARIS_WORKER_PORT:-8208}:worker-service"
fi
# Probe the services THIS release runs — the ones its gateway routes to. A
# rollback to a release without commerce-, portal- or user-service would
# otherwise fail on ports nothing is meant to answer. Its code being in the tree
# proves nothing: 3.1.2 shipped user-service code it neither ran nor routed.
_GW_TEMPLATE="$VELARIS_DIR/deploy/docker-compose/gateway/nginx.conf.template"
_routed() { grep -qE "^[[:space:]]*upstream[[:space:]]+$1[[:space:]]" "$_GW_TEMPLATE" 2>/dev/null; }
_SVC_PROBES="${VELARIS_CASE_PORT:-8201}:case-service"
_routed analytics_service && _SVC_PROBES="$_SVC_PROBES ${VELARIS_ANALYTICS_PORT:-8202}:analytics-service"
_routed hxgraph_service   && _SVC_PROBES="$_SVC_PROBES ${VELARIS_HXGRAPH_PORT:-8203}:hxgraph-service"
_routed hxmigrate_service && _SVC_PROBES="$_SVC_PROBES ${VELARIS_HXMIGRATE_PORT:-8204}:hxmigrate-service"
_routed commerce_service  && _SVC_PROBES="$_SVC_PROBES ${VELARIS_COMMERCE_PORT:-8207}:commerce-service"
_routed portal_service    && _SVC_PROBES="$_SVC_PROBES ${VELARIS_PORTAL_PORT:-8205}:portal-service"
_routed user_service      && _SVC_PROBES="$_SVC_PROBES ${VELARIS_USER_PORT:-8206}:user-service"

# The gateway itself is plain http on loopback whatever internal TLS says:
# _velaris_probe switches to https + client certificate when TLS is on, which
# the gateway never speaks — every update of a TLS-on install was HALTed as
# "health check failed: gateway" on a healthy platform (R3, 2026-11-01).
_gateway_probe() { curl -sf --max-time 3 "http://localhost:${SERVICE_PORT}/health" >/dev/null 2>&1; }

# /health on each port does not prove the ROUTES work: a gateway on the wrong
# config answers /health and 502s everything else. One anonymous request per
# API area, through the gateway; any answer from the app (401/403/404/422) is
# fine, 502/503/504 or no answer is not.
_routes_probe() {
  local path code bad=""
  for path in /api/v1/cases /api/v1/auth/real/me /api/v1/analytics/_probe /api/v1/graph/_probe \
              /api/v1/hxmigrate/_probe /api/v1/checkout/_probe /api/v1/portal/_probe/_probe; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:${SERVICE_PORT}${path}" 2>/dev/null || true)
    case "$code" in 000|502|503|504) bad="$bad ${path}=${code}" ;; esac
  done
  [[ -z "$bad" ]] || { ROUTES_FAILED="$bad"; return 1; }
}

step "Post-update health check..."
HEALTH_OK=false
HEALTH_FAILED=""
for i in $(seq 1 30); do
  HEALTH_FAILED=""
  ROUTES_FAILED=""
  _gateway_probe || HEALTH_FAILED=" gateway"
  for _svc in $_SVC_PROBES $_WORKER_PROBE; do
    _port="${_svc%%:*}"; _name="${_svc##*:}"
    _velaris_probe "${_port}" \
      || HEALTH_FAILED="${HEALTH_FAILED} ${_name}"
  done
  [[ -z "$HEALTH_FAILED" ]] && { _routes_probe || HEALTH_FAILED=" routes:${ROUTES_FAILED}"; }
  if [[ -z "$HEALTH_FAILED" ]]; then
    HEALTH_OK=true
    break
  fi
  sleep 2
done

# A finished-off update came from the version the interrupted run started on,
# not from the one it had already checked out.
if [[ "$RESUMING" == true ]]; then
  [[ -n "$RESUME_FROM" ]] && PREVIOUS_VERSION="$RESUME_FROM"
else
PREVIOUS_VERSION="$CURRENT_VERSION"  # beacon records where we came from (rollback target)
fi
rm -f "$UPDATE_MARKER_FILE"          # every step ran; what follows is a verdict, not a stop
CURRENT_VERSION="$LATEST_VERSION"    # beacon reports the new version
rm -f "$UPDATE_REQUEST_FILE"         # request consumed (success or halt — admin re-requests)

if [[ "$HEALTH_OK" == false ]]; then
  red "  ✗ health check failed after the update:${HEALTH_FAILED}"
  red "    This environment is HALTED for human attention — backup at: $BACKUP_FILE"
  red "    Logs: tail -f $VELARIS_DIR/logs/velaris-<service>.log  (systemd: journalctl -u velaris-<service>)"
  _write_status "unhealthy" "updated to v$LATEST_VERSION but health check failed"
  exit 1
fi
green "  ✓ all services healthy"
if [[ "$RESTART_PENDING" == true ]]; then
  # Healthy, but on the PREVIOUS code: say so rather than report an update.
  red "  ✗ v$LATEST_VERSION is installed but NOT running — the services could not be restarted."
  _units="velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service"
  [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-commerce-service.service" ] && _units="$_units velaris-commerce-service"
  [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-portal-service.service" ] && _units="$_units velaris-portal-service"
  [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-user-service.service" ] && _units="$_units velaris-user-service"
  [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-worker-service.service" ] && _units="$_units velaris-worker-service"
  red "    Run:  sudo systemctl restart $_units velaris-studio"
  _write_status "restart_required" "v$LATEST_VERSION installed; services still run the previous code — restart them"
  exit 1
fi
if [[ "$REQUEST_ACTION" == "rollback" ]]; then
  _write_status "rolled_back" "rolled back to v$LATEST_VERSION"
else
  _write_status "updated" "updated to v$LATEST_VERSION"
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
green "║   Velaris updated → v$LATEST_VERSION (channel: $UPDATE_CHANNEL)"
echo "║   Backup kept at: .backups/"
echo "╚══════════════════════════════════════════════════════╝"
# R5c: a linked standby follows by itself — it sees the new version in its next
# copy (≤ 1 min), takes this server's code and installs its dependencies. Until
# it has, a promote there is refused. Nothing to do here; say where to look.
if [ -f "$VELARIS_DIR/.standby/main.json" ]; then
  echo "  Standby: it follows this update by itself within a few minutes —"
  echo "           ./standby-velaris.sh status  (code: ok, same version on both)"
fi

# R4: what is working, degraded and off after the update — one line each for
# anything degraded, with the reason. Advisory: bounded, and never fails the
# update (the update already succeeded; this only says what it left behind).
if [[ -x "$VELARIS_DIR/.venv/bin/python" && -f "$VELARIS_DIR/services/case-service/case_service/readiness.py" ]]; then
  ( cd "$VELARIS_DIR" && PYTHONPATH="$VELARIS_DIR/services/case-service:$VELARIS_DIR/services/_shared${PYTHONPATH:+:$PYTHONPATH}" \
      timeout 60 "$VELARIS_DIR/.venv/bin/python" -m case_service.readiness 2>/dev/null ) \
    | sed 's/^/  /' || true
fi
