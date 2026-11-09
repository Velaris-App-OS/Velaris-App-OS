#!/usr/bin/env bash
# Derive the environment start-velaris.sh computes, then exec the real command.
#
#   velaris-exec.sh <VELARIS_DIR> <command...>
#
# The units load .env via EnvironmentFile=, but two variables are NOT in .env —
# start-velaris.sh derives them at runtime, and a service that skipped them
# would behave differently under systemd than under the script:
#
#   DATABASE_BACKEND       read from velaris.yaml. velaris_shared/config.py reads
#                          it (AliasChoices DATABASE_BACKEND) and DEFAULTS TO
#                          postgresql when absent — so on a MySQL/MariaDB
#                          install, omitting it would silently dispatch the
#                          Postgres paths (e.g. the case-number sequence uses
#                          nextval, which no-ops against MySQL).
#   VELARIS_SERVICE_REGISTRY the engine's HTTP task resolver reads it; absent
#                          means the built-in demo endpoints.
#
# Deriving them here rather than baking them in at install time means editing
# velaris.yaml and restarting the unit is enough — no reinstall, no drift.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

VELARIS_DIR="${1:?usage: velaris-exec.sh <VELARIS_DIR> <command...>}"
shift

# Same parsing as start-velaris.sh::_yaml_val, same fallback.
_yaml_val() {
  grep -E "^\s+${1}:" "$VELARIS_DIR/velaris.yaml" 2>/dev/null \
    | head -1 | sed -E "s/.*${1}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" | xargs || echo "${2:-}"
}

DATABASE_BACKEND="$(_yaml_val "database" "postgresql")"
case "$DATABASE_BACKEND" in
  postgresql|mysql|mariadb) ;;
  *)
    echo "STARTUP BLOCKED: unsupported database backend '$DATABASE_BACKEND' in velaris.yaml" >&2
    exit 1
    ;;
esac
export DATABASE_BACKEND

# Parts on other servers (VELARIS_EXTERNAL_SERVICES) must be encrypted and
# authenticated; a unit refuses to start without it (the journal names what is
# missing). Nothing listed = nothing checked.
if [ -f "$VELARIS_DIR/deploy/external-services.sh" ]; then
  # shellcheck source=/dev/null
  VELARIS_DIR="$VELARIS_DIR" . "$VELARIS_DIR/deploy/external-services.sh"
  velaris_external_check || exit 1
fi

export VELARIS_SERVICE_REGISTRY="${VELARIS_SERVICE_REGISTRY:-{\"order-service\":\"http://localhost:3001\",\"notification-service\":\"http://localhost:3002\"}}"

# Internal TLS (#18) — appended here rather than baked into each unit's
# ExecStart, so turning it on is one .env line and not five unit edits.
# The service name comes from the uvicorn module argument, which is the one
# thing every unit's command line already carries.
_svc=""
for _a in "$@"; do
  case "$_a" in
    case_service.main:app)      _svc="case-service" ;;
    analytics_service.main:app) _svc="analytics-service" ;;
    hxgraph_service.main:app)   _svc="hxgraph-service" ;;
    hxmigrate_service.main:app) _svc="hxmigrate-service" ;;
    commerce_service.main:app)  _svc="commerce-service" ;;
    portal_service.main:app)    _svc="portal-service" ;;
    user_service.main:app)      _svc="user-service" ;;
    case_service.worker.main:app) _svc="worker-service" ;;
  esac
done
# A unit's port comes from .env through ${VELARIS_*_PORT} in its ExecStart:
# `VELARIS_CASE_PORT=` there overrode the unit's default and uvicorn got
# `--port ''`; a typo stopped the service. Either is now the default (R1).
_port_default=""
for _a in "$@"; do
  case "$_a" in
    helix_engine.main:app) _port_default=8100 ;;
    preview)               _port_default=5173 ;;
  esac
done
case "$_svc" in
  case-service) _port_default=8201 ;;      analytics-service) _port_default=8202 ;;
  hxgraph-service) _port_default=8203 ;;   hxmigrate-service) _port_default=8204 ;;
  portal-service) _port_default=8205 ;;    user-service) _port_default=8206 ;;
  commerce-service) _port_default=8207 ;;  worker-service) _port_default=8208 ;;
esac
[ -f "$VELARIS_DIR/deploy/ports.sh" ] && . "$VELARIS_DIR/deploy/ports.sh"
if [ -n "$_port_default" ] && type _velaris_port_ok >/dev/null 2>&1; then
  _argv=(); _prev=""
  for _a in "$@"; do
    if [ "$_prev" = "--port" ] && ! _velaris_port_ok "$_a"; then
      echo "--port '$_a' is not a port — using $_port_default" >&2
      _a="$_port_default"
    fi
    _argv+=("$_a"); _prev="$_a"
  done
  set -- "${_argv[@]}"
fi

# ── extracted services before their own unit is installed (R5e steps 7, 8, 10) ──
# An update cannot install a systemd unit: the update agent runs as this
# install's user, not root. Until `sudo deploy/systemd/install.sh` has installed
# velaris-commerce-service.service / velaris-portal-service.service /
# velaris-user-service.service, case-service's unit runs that service beside it,
# so an unattended update never leaves checkout, payments, storefront,
# marketplace, the model market, the customer portal — or sign-in — answering
# 502 (user's decision, 2026-10-19). It lives in
# case-service's control group: stopping or restarting case-service stops it
# too, and a crash is restarted by the loop. With the unit installed this never
# runs for that service.
_unit_dir="${VELARIS_UNIT_DIR:-/etc/systemd/system}"
_velaris_companion() {   # <service-name> <package> <port>
  _c_svc="$1"; _c_pkg="$2"; _c_port="$3"
  [ -d "$VELARIS_DIR/services/$_c_svc" ] || return 0
  [ -f "$_unit_dir/velaris-$_c_svc.service" ] && return 0
  _c_tls=""
  if [ -f "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh" ]; then
    . "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh"
    _c_tls="$(VELARIS_DIR="$VELARIS_DIR" _velaris_tls_args "$_c_svc")"
  fi
  echo "$_c_svc unit not installed — running it with case-service" \
       "(install it: sudo deploy/systemd/install.sh)" >&2
  (
    cd "$VELARIS_DIR"
    export VELARIS_SERVICE_NAME="$_c_svc"
    # systemd's stop signal reaches the whole control group; leave the loop on
    # it instead of restarting into the 20s stop timeout.
    trap 'exit 0' INT TERM
    while true; do
      # shellcheck disable=SC2086 - the TLS flags are deliberately word-split
      uv run uvicorn "$_c_pkg.main:app" --host 127.0.0.1 \
        --port "$_c_port" --app-dir "services/$_c_svc" \
        --proxy-headers $_c_tls || true
      echo "$_c_svc (with case-service) exited — restarting in 5s" >&2
      sleep 5
    done
  ) &
}
type _velaris_port >/dev/null 2>&1 || _velaris_port() { local v="${!1:-}"; printf '%s' "${v:-$2}"; }
if [ "$_svc" = "case-service" ] && [ -n "${INVOCATION_ID:-}" ]; then
  _velaris_companion commerce-service commerce_service "$(_velaris_port VELARIS_COMMERCE_PORT 8207)"
  _velaris_companion portal-service portal_service "$(_velaris_port VELARIS_PORTAL_PORT 8205)"
  _velaris_companion user-service user_service "$(_velaris_port VELARIS_USER_PORT 8206)"
fi

# Processes per service (VELARIS_WORKERS_<SERVICE>) — nothing when unset or 1.
_workers_args=""
if [ -n "$_svc" ] && [ -f "$VELARIS_DIR/deploy/workers-args.sh" ]; then
  # shellcheck source=/dev/null
  . "$VELARIS_DIR/deploy/workers-args.sh"
  _workers_args="$(_velaris_workers_args "$_svc")"
  [ -n "$_workers_args" ] && echo "$_svc: $_workers_args" >&2
fi

if [ -n "$_svc" ] && [ -f "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh" ]; then
  # shellcheck source=/dev/null
  . "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh"
  _tls_args="$(VELARIS_DIR="$VELARIS_DIR" _velaris_tls_args "$_svc")"
  if [ -n "$_tls_args" ]; then
    echo "Internal TLS enabled for $_svc" >&2
    # shellcheck disable=SC2086 - deliberate word splitting; the values are ours
    exec "$@" $_tls_args $_workers_args
  fi
fi

# shellcheck disable=SC2086 - deliberate word splitting; the value is ours
exec "$@" $_workers_args
