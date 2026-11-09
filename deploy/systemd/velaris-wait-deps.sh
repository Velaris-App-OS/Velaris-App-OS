#!/usr/bin/env bash
# Block until the backing services a Velaris unit needs are actually accepting
# connections, then exit 0. Used as ExecStartPre= in the engine and case-service
# units.
#
#   velaris-wait-deps.sh <VELARIS_DIR>
#
# Why this exists (2026-08-05): the units already ordered themselves After=
# docker.service, but that only means the DAEMON is up — not that Postgres
# inside a container has finished booting and bound its port. On that day the
# containers had no restart policy at all, so the daemon came back alone and
# case-service started against a database that would never arrive: it stayed
# "active (running)" and answered every login with a 500 for 30 minutes.
#
# Ordering alone cannot express "the port is open". This can. Failing here is
# the point: a failed ExecStartPre puts the unit in failed state, and with
# Restart=always + StartLimitIntervalSec=0 systemd retries every RestartSec
# forever — so a slow or briefly-missing database self-heals into a working
# platform instead of a permanently broken one.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -uo pipefail

VELARIS_DIR="${1:?usage: velaris-wait-deps.sh <VELARIS_DIR>}"
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
TIMEOUT="${VELARIS_WAIT_DEPS_TIMEOUT:-90}"

# The unit loads .env as EnvironmentFile, but this script also runs by hand
# during triage, when nothing is exported. Read it directly if we must.
if [ -z "${VELARIS_CASE_DATABASE_URL:-}" ] && [ -f "$VELARIS_DIR/.env" ]; then
  VELARIS_CASE_DATABASE_URL="$(grep -E '^VELARIS_CASE_DATABASE_URL=' "$VELARIS_DIR/.env" \
    | tail -1 | cut -d= -f2- | tr -d '"'"'"'')"
fi

if [ -z "${VELARIS_CASE_DATABASE_URL:-}" ]; then
  echo "wait-deps: no VELARIS_CASE_DATABASE_URL — cannot verify the database, continuing" >&2
  exit 0
fi

# postgresql+asyncpg://user:pass@host:port/db  ->  host, port
# Strip the scheme, then everything up to the LAST '@' — passwords legitimately
# contain '@' and ':', so anything anchored on the first one parses the wrong
# host on a machine with a strong password and works fine on a weak one.
_netloc="${VELARIS_CASE_DATABASE_URL#*://}"
_netloc="${_netloc%%/*}"
_hostport="${_netloc##*@}"
DB_HOST="${_hostport%%:*}"
DB_PORT="${_hostport##*:}"

if [ "$DB_PORT" = "$DB_HOST" ]; then           # no ':port' in the URL
  case "$VELARIS_CASE_DATABASE_URL" in
    mysql*|mariadb*) DB_PORT=3306 ;;
    *)               DB_PORT=5432 ;;
  esac
fi
[ -n "$DB_HOST" ] || DB_HOST=127.0.0.1

# bash's /dev/tcp rather than nc: one less package to depend on inside a unit
# that runs with a minimal PATH and no shell profile.
_port_open() {
  timeout 2 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
}

echo "wait-deps: waiting up to ${TIMEOUT}s for database $DB_HOST:$DB_PORT"
_deadline=$(( SECONDS + TIMEOUT ))
until _port_open "$DB_HOST" "$DB_PORT"; do
  if [ "$SECONDS" -ge "$_deadline" ]; then
    echo "wait-deps: database $DB_HOST:$DB_PORT still refusing after ${TIMEOUT}s." >&2
    echo "wait-deps: check  docker ps -a --format '{{.Names}}\t{{.Status}}'  — if velaris-db is" >&2
    echo "wait-deps: Exited, bring the stack up:  docker compose -f $VELARIS_DIR/deploy/docker-compose/docker-compose.yml --env-file $VELARIS_DIR/.env up -d" >&2
    exit 1
  fi
  sleep 2
done
echo "wait-deps: database $DB_HOST:$DB_PORT is accepting connections"

# The connection pooler (R5d 6a), when the platform is set to go through it.
# Unset or empty = nothing to wait for, as before. PostgreSQL only — on MySQL /
# MariaDB the platform ignores the setting, so waiting on it would only block.
if [ -z "${VELARIS_CASE_DATABASE_POOLER_URL:-}" ] && [ -f "$VELARIS_DIR/.env" ]; then
  VELARIS_CASE_DATABASE_POOLER_URL="$(grep -E '^VELARIS_CASE_DATABASE_POOLER_URL=' "$VELARIS_DIR/.env" \
    | tail -1 | cut -d= -f2- | tr -d '"'"'"'')"
fi
case "$VELARIS_CASE_DATABASE_URL" in mysql*|mariadb*) VELARIS_CASE_DATABASE_POOLER_URL="" ;; esac
if [ -n "${VELARIS_CASE_DATABASE_POOLER_URL:-}" ]; then
  _netloc="${VELARIS_CASE_DATABASE_POOLER_URL#*://}"
  _netloc="${_netloc%%/*}"
  _hostport="${_netloc##*@}"
  PL_HOST="${_hostport%%:*}"
  PL_PORT="${_hostport##*:}"
  [ "$PL_PORT" = "$PL_HOST" ] && PL_PORT=5432
  [ -n "$PL_HOST" ] || PL_HOST=127.0.0.1
  # Over its UNIX socket when the install says so: the platform connects there,
  # never to the port (the pooler's TCP port can be open while the socket is not).
  _psock="${VELARIS_CASE_DATABASE_POOLER_SOCKET:-}"
  if [ -z "$_psock" ] && [ -f "$VELARIS_DIR/.env" ]; then
    _psock="$(grep -E '^VELARIS_CASE_DATABASE_POOLER_SOCKET=' "$VELARIS_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
  fi
  _sock="$VELARIS_DIR/deploy/docker-compose/pooler-socket/.s.PGSQL.6432"
  case "$(printf '%s' "$_psock" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) _pooler_ready() { [ -S "$_sock" ]; }; PL_WHAT="$_sock"
                   PL_DOWN="still not there"; PL_UP="is ready" ;;
    *)             _pooler_ready() { _port_open "$PL_HOST" "$PL_PORT"; }; PL_WHAT="$PL_HOST:$PL_PORT"
                   PL_DOWN="still refusing"; PL_UP="is accepting connections" ;;
  esac
  echo "wait-deps: waiting up to ${TIMEOUT}s for the connection pooler $PL_WHAT"
  _deadline=$(( SECONDS + TIMEOUT ))
  until _pooler_ready; do
    if [ "$SECONDS" -ge "$_deadline" ]; then
      echo "wait-deps: connection pooler $PL_WHAT $PL_DOWN after ${TIMEOUT}s." >&2
      echo "wait-deps: check  docker compose -f $VELARIS_DIR/deploy/docker-compose/docker-compose.yml --env-file $VELARIS_DIR/.env logs velaris-pooler" >&2
      echo "wait-deps: or empty VELARIS_CASE_DATABASE_POOLER_URL in .env to connect directly." >&2
      exit 1
    fi
    sleep 2
  done
  echo "wait-deps: connection pooler $PL_WHAT $PL_UP"
fi

# OpenBao comes back SEALED after every reboot. start-velaris.sh unseals it,
# but a boot through these units never ran that, so it stayed sealed until
# someone noticed (2026-10-22, 10-23: the nightly backup went out without it).
# Soft, like Temporal: unseal from the local keyfile when this install uses
# OpenBao; never blocks the platform, never prints key material. No
# deploy/openbao/enabled = OpenBao not in use = nothing happens, as before.
if [ -f "$VELARIS_DIR/deploy/openbao/enabled" ] && [ -x "$VELARIS_DIR/scripts/secrets-unseal.sh" ]; then
  for _i in $(seq 1 15); do
    _bao="$(curl -s -m 2 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:8350/v1/sys/health?sealedcode=472&uninitcode=471" || true)"
    [ "$_bao" != "000" ] && break
    sleep 2
  done
  if [ "$_bao" = "472" ]; then
    if "$VELARIS_DIR/scripts/secrets-unseal.sh" >/dev/null 2>&1; then
      echo "wait-deps: OpenBao unsealed from the local keyfile"
    else
      echo "wait-deps: OpenBao is sealed and could not be unsealed — run ./scripts/secrets-unseal.sh" >&2
    fi
  fi
fi

# Temporal is a soft dependency: the workflow poller retries on its own and the
# API serves requests without it, so a missing Temporal must not block boot the
# way a missing database does. Wait briefly, then note it and move on. Where it
# runs on another server, its address comes from the same settings the clients
# read (deploy/external-services.sh).
_temporal="127.0.0.1:7233"
if [ -f "$VELARIS_DIR/deploy/external-services.sh" ]; then
  # shellcheck source=/dev/null
  VELARIS_DIR="$VELARIS_DIR" . "$VELARIS_DIR/deploy/external-services.sh"
  _temporal="$(velaris_temporal_target)"
fi
_deadline=$(( SECONDS + 30 ))
until _port_open "${_temporal%:*}" "${_temporal##*:}"; do
  if [ "$SECONDS" -ge "$_deadline" ]; then
    echo "wait-deps: Temporal $_temporal not up after 30s — starting anyway (poller will retry)" >&2
    break
  fi
  sleep 2
done

exit 0
