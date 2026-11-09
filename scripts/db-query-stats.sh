#!/usr/bin/env bash
# Query statistics (pg_stat_statements) for the bundled PostgreSQL — what
# HxDBManager's slow-query list and index advisor read.
#
#   VELARIS_DB_QUERY_STATS=true    on   (setup-velaris.sh writes this for a NEW install)
#   VELARIS_DB_QUERY_STATS=false   off  (removes what `true` added)
#   unset / empty                  nothing is changed — exactly as before
#
#   ./scripts/db-query-stats.sh            apply the setting (start, update and setup run it)
#   ./scripts/db-query-stats.sh --status   say what is loaded, change nothing
#
# Turning it on or off loads or unloads a server library, which takes a
# RESTART of the database container (a few seconds) — once, only when the
# setting and the server disagree. Kept in the database's own volume
# (ALTER SYSTEM → postgresql.auto.conf), so an update or a container recreate
# keeps it; other preload libraries an operator added are left in place.
#
# Only the bundled PostgreSQL: a database on another server (managed or your
# own) is turned on in its own configuration (e.g. a parameter group), and
# MySQL / MariaDB have performance_schema instead.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$VELARIS_DIR/.env"
DB_CONTAINER="${VELARIS_DB_CONTAINER:-docker-compose-velaris-db-1}"
LIB=pg_stat_statements
MODE="${1:-apply}"

say()  { [ "${QUIET:-}" = 1 ] || echo "$*"; }
warn() { echo "$*" >&2; }

_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true; }

[ -f "$ENV_FILE" ] || { say "  query stats: no .env — nothing to do"; exit 0; }
want="$(_env VELARIS_DB_QUERY_STATS | tr '[:upper:]' '[:lower:]')"
case "$want" in
  1|true|yes|on)  want=on ;;
  0|false|no|off) want=off ;;
  "")             want="" ;;
  *) warn "  ✗ VELARIS_DB_QUERY_STATS=$want — use true or false"; exit 1 ;;
esac
[ "$MODE" = --status ] || [ -n "$want" ] || exit 0       # unset: as before, untouched

backend="$(_env VELARIS_CASE_DATABASE_BACKEND | tr '[:upper:]' '[:lower:]')"
case "${backend:-postgresql}" in
  postgresql|postgres) ;;
  *) say "  query stats: $backend uses performance_schema — VELARIS_DB_QUERY_STATS applies to PostgreSQL only"; exit 0 ;;
esac
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/external-services.sh" ] && . "$VELARIS_DIR/deploy/external-services.sh"
if type velaris_is_external >/dev/null 2>&1 && velaris_is_external database 2>/dev/null; then
  say "  query stats: the database is on another server — turn $LIB on in its own configuration"
  exit 0
fi
if [ "$(docker inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null)" != true ]; then
  warn "  ✗ query stats: $DB_CONTAINER is not running — start the platform, then run this again"
  exit 1
fi

q() { docker exec "$DB_CONTAINER" psql -U velaris -d velaris -tAXq -v ON_ERROR_STOP=1 -c "$1"; }

current="$(q "SHOW shared_preload_libraries" | tr -d ' ')"
loaded=no; case ",$current," in *",$LIB,"*) loaded=yes ;; esac
ext="$(q "SELECT count(*) FROM pg_extension WHERE extname = '$LIB'")"

if [ "$MODE" = --status ]; then
  say "  query stats: setting=${want:-unset}, library loaded=$loaded, extension=$([ "$ext" = 1 ] && echo yes || echo no)"
  exit 0
fi

restart_db() {
  say "  ▶ restarting the database to $1 $LIB (a few seconds)..."
  docker restart "$DB_CONTAINER" >/dev/null
  for _ in $(seq 1 60); do
    docker exec "$DB_CONTAINER" pg_isready -U velaris -q 2>/dev/null && return 0
    sleep 1
  done
  warn "  ✗ the database did not come back after the restart — docker logs $DB_CONTAINER"
  exit 1
}

if [ "$(q "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")" != t ]; then
  warn "  ✗ query stats: the velaris role cannot change server settings here — set shared_preload_libraries=$LIB by hand"
  exit 1
fi

if [ "$want" = on ]; then
  if [ "$loaded" = no ]; then
    list="${current:+$current,}$LIB"
    q "ALTER SYSTEM SET shared_preload_libraries = '$list'" >/dev/null
    restart_db load
  fi
  [ "$ext" = 1 ] || q "CREATE EXTENSION IF NOT EXISTS $LIB" >/dev/null
  [ "$loaded" = yes ] && [ "$ext" = 1 ] || say "  ✓ query stats on ($LIB) — HxDBManager's slow queries and index advisor can read it"
else
  [ "$ext" = 0 ] || q "DROP EXTENSION IF EXISTS $LIB" >/dev/null
  if [ "$loaded" = yes ]; then
    rest="$(printf '%s' "$current" | tr ',' '\n' | grep -vx "$LIB" | paste -sd, - || true)"
    if [ -n "$rest" ]; then q "ALTER SYSTEM SET shared_preload_libraries = '$rest'" >/dev/null
    else q "ALTER SYSTEM RESET shared_preload_libraries" >/dev/null; fi
    restart_db unload
  fi
  [ "$loaded" = no ] && [ "$ext" = 0 ] || say "  ✓ query stats off"
fi
