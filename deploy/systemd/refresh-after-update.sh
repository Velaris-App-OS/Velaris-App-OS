#!/usr/bin/env bash
# Velaris under systemd — finish an update: the three things nothing else does.
#
# Why this exists
# ---------------
# `update-velaris.sh` ends by calling `start-velaris.sh`. On an install that
# systemd supervises, `start-velaris.sh` sees the running units and exits at
# once — correctly, a second copy from a shell would race them for the ports.
# But that early exit is BEFORE everything it does besides starting processes,
# so until 2026-10-11 an update on a systemd install:
#
#   1. never re-applied the release manifest (releases/manifest.sql);
#   2. never rebuilt Studio — the unit serves the built `dist` and builds only
#      when `dist` is MISSING, so Studio stayed on the previous release for good;
#   3. never restarted anything — new code on disk, old processes in memory —
#      and then passed its own health check against those old processes and
#      reported "updated".
#
# This script does those three, in that order, and says plainly when it could
# not do the last one. `update-velaris.sh` calls it instead of
# `start-velaris.sh` when the units are active. It is safe to run by hand, any
# number of times.
#
# Usage
# -----
#   deploy/systemd/refresh-after-update.sh                    # all three
#   deploy/systemd/refresh-after-update.sh --non-interactive  # never prompt (timer)
#   deploy/systemd/refresh-after-update.sh --no-restart       # 1 and 2 only
#
# Exit status
# -----------
#   0  done (or nothing to do: systemd does not run Velaris here)
#   3  manifest and Studio handled, but the services could NOT be restarted —
#      the command to run is printed. The update is NOT live until it is.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

set -uo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VELARIS_DIR="$(cd "$SRC_DIR/../.." && pwd)"
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
UNIT_DIR="${VELARIS_UNIT_DIR:-/etc/systemd/system}"
# Scratch files and error logs go to a directory only this user can write. A
# fixed name under /tmp can be pre-created, or pointed somewhere else, by any
# other local user before this script writes to it.
VELARIS_SCRATCH="${VELARIS_SCRATCH:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/velaris}"
if ! { mkdir -p "$VELARIS_SCRATCH" && chmod 700 "$VELARIS_SCRATCH" && [ -w "$VELARIS_SCRATCH" ]; } 2>/dev/null; then
  VELARIS_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/velaris.XXXXXX")"
fi
UNITS=(velaris-engine velaris-case-service velaris-analytics-service
       velaris-hxgraph-service velaris-hxmigrate-service velaris-commerce-service
       velaris-portal-service velaris-user-service velaris-worker-service velaris-studio)

INTERACTIVE=true
DO_RESTART=true
for arg in "$@"; do
  case "$arg" in
    --non-interactive) INTERACTIVE=false ;;
    --no-restart)      DO_RESTART=false ;;
    -h|--help) sed -n '2,36p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
red()    { echo -e "\033[0;31m$*\033[0m"; }

ACTIVE=()
for u in "${UNITS[@]}"; do
  systemctl is-active --quiet "$u" 2>/dev/null && ACTIVE+=("$u")
done
if [ "${#ACTIVE[@]}" -eq 0 ]; then
  echo "  systemd is not running Velaris here — nothing to refresh."
  exit 0
fi

# ── 1. Release manifest ───────────────────────────────────────────
# The same idempotent upsert start-velaris.sh applies on every start. A failure
# is reported and does not stop the refresh: a feature row that did not land is
# not a reason to leave the services on old code.
DB_BACKEND="$(grep -E '^\s+database:' "$VELARIS_DIR/velaris.yaml" 2>/dev/null | head -1 \
              | sed -E 's/.*database:[[:space:]]*"?([a-z]+)"?.*/\1/')"
DB_CONTAINER="${DB_CONTAINER:-docker-compose-velaris-db-1}"
DB_USER="${DB_USER:-velaris}"
DB_NAME="${DB_NAME:-velaris}"
type velaris_psql >/dev/null 2>&1 \
  || velaris_psql() { docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" "$@"; }
# ── Infrastructure containers ─────────────────────────────────────
# Under systemd nothing else brings the compose stack to the new release: the
# update recreates only the database container, and start-velaris.sh (which
# runs `compose up`) exits at once here. Until 2026-10-16 a systemd install kept
# its previous containers after every update — pinned images never applied,
# and the November changes (OpenBao on raft, Redpanda replaced by Kafka) would
# not have happened. The same steps start-velaris.sh takes, in its order.
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
_compose() { docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" "$@"; }
echo "▶ Infrastructure containers..."
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh" ] \
  && VELARIS_DIR="$VELARIS_DIR" . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"
if [ -f "$VELARIS_DIR/deploy/openbao/enabled" ]; then
  "$VELARIS_DIR/scripts/openbao-storage-migrate.sh" \
    || yellow "  ⚠ OpenBao storage migration failed — see above (docs/recovery-runbook.md §6)"
fi
type velaris_retire_redpanda >/dev/null 2>&1 && velaris_retire_redpanda
type velaris_ensure_event_bus_password >/dev/null 2>&1 && velaris_ensure_event_bus_password
type velaris_ensure_cache_password >/dev/null 2>&1 && velaris_ensure_cache_password
# Parts on other servers (VELARIS_EXTERNAL_SERVICES): refused when unsecured;
# their bundled containers are stopped, not started. Nothing external = `up -d`.
if type velaris_external_check >/dev/null 2>&1 && ! velaris_external_check; then
  exit 1
fi
# deploy/tls/certs before Docker can create it as root (the gateway mounts it).
# shellcheck source=deploy/tls/certs-dir.sh
[ -f "$VELARIS_DIR/deploy/tls/certs-dir.sh" ] && . "$VELARIS_DIR/deploy/tls/certs-dir.sh" && velaris_tls_certs_dir "$VELARIS_DIR"
_UP_SVCS=""
if type velaris_compose_up_services >/dev/null 2>&1; then
  velaris_retire_external_containers "$COMPOSE_FILE" "$VELARIS_DIR/.env"
  _UP_SVCS="$(velaris_compose_up_services "$COMPOSE_FILE" "$VELARIS_DIR/.env")" || exit 1
fi
# shellcheck disable=SC2086
if _compose up -d $_UP_SVCS >"$VELARIS_SCRATCH/velaris-compose-up.log" 2>&1; then
  green "  ✓ containers on this release's images"
else
  yellow "  ⚠ compose up had errors:"; tail -5 "$VELARIS_SCRATCH/velaris-compose-up.log"
fi
if [ -f "$VELARIS_DIR/deploy/openbao/enabled" ]; then
  # A recreated OpenBao comes back sealed; the platform runs on the rendered
  # .env either way, but day-2 secret writes need it open.
  for _i in $(seq 1 20); do
    _h=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8350/v1/sys/health?sealedcode=472&uninitcode=471" || true)
    [ "$_h" != "000" ] && break; sleep 1
  done
  if "$VELARIS_DIR/scripts/secrets-unseal.sh" >/dev/null 2>&1; then green "  ✓ OpenBao unsealed"
  else yellow "  ⚠ OpenBao still sealed — ./scripts/secrets-unseal.sh"; fi
fi
# The internal service secret and the backup key are generated by update-velaris.sh
# Step 6b — but an update FROM a release older than them runs that release's
# updater, which knows neither; this script is the only part of the new tree it
# runs. Found 2026-10-20 updating v3.1.2: both missing, so commerce-service's
# calls to /internal/v1 were refused. Both are no-ops when already set.
# After the unseal above, not before: each also writes the new value into
# OpenBao when the vault holds the secrets, and a sealed (or not yet recreated)
# vault refused both — R3 2026-11-01: "Could not store it in OpenBao", and the
# next render then refused to drop the two keys .env had and the vault lacked.
type velaris_ensure_internal_secret >/dev/null 2>&1 && velaris_ensure_internal_secret
if [[ -x "$VELARIS_DIR/.venv/bin/python" && -f "$VELARIS_DIR/scripts/backup/velaris_backup.py" ]]; then
  "$VELARIS_DIR/.venv/bin/python" "$VELARIS_DIR/scripts/backup/velaris_backup.py" ensure-key \
    || yellow "  ⚠ Backup key not set — run ./backup-velaris.sh --ensure-key"
fi

echo "▶ Release manifest..."
case "${DB_BACKEND:-postgresql}" in
  postgresql)
    if [ -f "$VELARIS_DIR/releases/manifest.sql" ]; then
      if velaris_psql -q \
           < "$VELARIS_DIR/releases/manifest.sql" 2>"$VELARIS_SCRATCH/velaris-manifest-err.log" \
         && ! grep -q "ERROR" "$VELARIS_SCRATCH/velaris-manifest-err.log"; then
        green "  ✓ release manifest applied"
      else
        yellow "  ⚠ release manifest had errors — feature versions may be stale:"
        head -5 "$VELARIS_SCRATCH/velaris-manifest-err.log"
      fi
    fi ;;
  mysql|mariadb)
    # Through deploy/external-services.sh's client (loaded with compose-profiles.sh).
    if [ -f "$VELARIS_DIR/releases/manifest.mysql.sql" ] && type velaris_mysql >/dev/null 2>&1; then
      if velaris_mysql < "$VELARIS_DIR/releases/manifest.mysql.sql" 2>"$VELARIS_SCRATCH/velaris-manifest-err.log"; then
        green "  ✓ release manifest applied"
      else
        yellow "  ⚠ release manifest had errors — feature versions may be stale:"
        grep -v "Using a password" "$VELARIS_SCRATCH/velaris-manifest-err.log" | head -5
      fi
    elif [ -f "$VELARIS_DIR/releases/manifest.mysql.sql" ]; then
      yellow "  ⚠ database is ${DB_BACKEND}: apply releases/manifest.mysql.sql by hand"
    fi ;;
  *)
    yellow "  ⚠ database is ${DB_BACKEND}: no release manifest for it" ;;
esac

# ── 2. Studio ─────────────────────────────────────────────────────
# Built beside the live copy and swapped in, so Studio is never served from a
# half-written directory, and a failed build leaves the working one in place.
build_studio() {
  local studio="$VELARIS_DIR/studio" npm_bin
  [ -f "$studio/package.json" ] || return 0
  npm_bin="$(command -v npm 2>/dev/null || true)"
  if [ -z "$npm_bin" ] && [ -f "$UNIT_DIR/velaris-studio.service" ]; then
    # systemd has no shell profile; the unit carries the absolute path.
    npm_bin="$(grep -E '^ExecStart=' "$UNIT_DIR/velaris-studio.service" \
               | grep -oE '[^ ]*/npm( |$)' | head -1 | tr -d ' ')"
  fi
  if [ -z "$npm_bin" ] || [ ! -x "$npm_bin" ]; then
    red "  ✗ npm not found — Studio NOT rebuilt; it still serves the previous release"
    return 1
  fi
  rm -rf "$studio/dist.new"
  if ( cd "$studio" && PATH="$(dirname "$npm_bin"):$PATH" \
         "$npm_bin" run build -- --outDir dist.new --emptyOutDir ) \
       >"$VELARIS_SCRATCH/velaris-studio-build.log" 2>&1 && [ -f "$studio/dist.new/index.html" ]; then
    rm -rf "$studio/dist.old"
    [ -d "$studio/dist" ] && mv "$studio/dist" "$studio/dist.old"
    mv "$studio/dist.new" "$studio/dist"
    rm -rf "$studio/dist.old"
    green "  ✓ Studio rebuilt"
  else
    rm -rf "$studio/dist.new"
    red "  ✗ Studio build FAILED — still serving the previous release."
    red "    Log: $VELARIS_SCRATCH/velaris-studio-build.log"
    tail -5 "$VELARIS_SCRATCH/velaris-studio-build.log"
    return 1
  fi
}
STUDIO_OK=true
if printf '%s\n' "${ACTIVE[@]}" | grep -qx velaris-studio; then
  echo "▶ Studio..."
  build_studio || STUDIO_OK=false
fi

# ── 3. Restart ────────────────────────────────────────────────────
[ "$DO_RESTART" = true ] || { echo "  (--no-restart: services left running)"; exit 0; }

RESTART_CMD="sudo systemctl restart ${ACTIVE[*]}"
echo "▶ Restarting: ${ACTIVE[*]}"

restarted=false
if [ "$(id -u)" -eq 0 ]; then
  systemctl restart "${ACTIVE[@]}" && restarted=true
elif sudo -n true 2>/dev/null; then
  sudo -n systemctl restart "${ACTIVE[@]}" && restarted=true
elif [ "$INTERACTIVE" = true ] && [ -t 0 ]; then
  sudo systemctl restart "${ACTIVE[@]}" && restarted=true
fi

# No root and no prompt (the update timer runs as the install's user). The
# units are `Restart=always` and their processes are that user's own, so ending
# each main process hands the restart to systemd. Only done when BOTH hold for
# a unit; otherwise it is left running and named below.
if [ "$restarted" = false ]; then
  restarted=true
  for u in "${ACTIVE[@]}"; do
    pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"
    policy="$(systemctl show -p Restart --value "$u" 2>/dev/null)"
    if [ "$policy" != "always" ] || [ -z "$pid" ] || [ "$pid" = "0" ] \
       || [ "$(stat -c %u "/proc/$pid" 2>/dev/null)" != "$(id -u)" ]; then
      restarted=false; continue
    fi
    kill -TERM "$pid" 2>/dev/null || { restarted=false; continue; }
    new="$pid"
    for _ in $(seq 1 30); do
      sleep 1
      new="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"
      [ -n "$new" ] && [ "$new" != "0" ] && [ "$new" != "$pid" ] && break
    done
    if [ "$new" = "$pid" ] || [ -z "$new" ] || [ "$new" = "0" ]; then
      red "  ✗ $u did not come back after being stopped"; restarted=false
    fi
  done
fi

if [ "$restarted" = true ]; then
  green "  ✓ services restarted"
  [ "$STUDIO_OK" = true ] || yellow "  ⚠ …but Studio was not rebuilt (see above)"
  exit 0
fi
red "  ✗ The services were NOT all restarted — they are still running the previous code."
red "    Run:  $RESTART_CMD"
exit 3
