#!/usr/bin/env bash
# Velaris — which optional parts of the compose file this install runs.
#
# SOURCE this before any `docker compose` call against docker-compose.yml:
#
#   . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"
#
# It exports COMPOSE_PROFILES, which every later `docker compose` in the same
# shell reads. One line per script, rather than a flag on every call — a flag
# forgotten on one call is a container that silently is not started, or is not
# stopped.
#
# The bundled MinIO (profile `minio`)
# -----------------------------------
# Until v3.1.2 every install ran a MinIO container for documents. MinIO's images
# were withdrawn from Docker Hub around 2026-09-11, so a NEW install can no
# longer pull one: from v3.1.2 a new install stores documents on local disk, and
# object storage is an S3 endpoint the operator points at
# (VELARIS_CASE_STORAGE_BACKEND=s3 + VELARIS_CASE_S3_*).
#
# An install that ALREADY runs the bundled MinIO must keep it: its documents are
# in that volume. So the profile is switched on by what the host holds, not by a
# setting somebody has to remember to add:
#
#   - the `velaris-minio` container exists, or
#   - a `…_velaris-minio-data` volume exists.
#
# The storage setting is deliberately NOT the test. `minio`/`s3` with an external
# endpoint is a legitimate new install, and starting the bundled container for
# it would fail on an image that cannot be pulled.
#
# Override, in the environment or in .env:
#   VELARIS_BUNDLED_MINIO=1   run it regardless (a restore onto a new host that
#                             already has the images)
#   VELARIS_BUNDLED_MINIO=0   never run it (after moving the documents off it —
#                             scripts/storage-migrate.py)
#
# The event broker (profile `events`)
# -----------------------------------
# Apache Kafka for the CDC event bus. Runs ONLY when the bus is on —
# VELARIS_CASE_EVENT_BUS_ENABLED=true (1/yes/on), in the environment or .env.
# Unset, empty or anything else: no broker, which is the default and matches
# the platform (the bus is off unless turned on).

# The connection pooler (profile `pooler`)
# ----------------------------------------
# PgBouncer in front of the bundled PostgreSQL (R5d 6a). Runs ONLY when
# VELARIS_CASE_DATABASE_POOLER_URL is set (environment or .env), the database
# is PostgreSQL (velaris.yaml `database:`), and the database is the bundled one
# (not in VELARIS_EXTERNAL_SERVICES — an external database brings its own
# pooler, if any). Unset or empty: no container, the platform connects directly.

# The replication gateway (profile `standby`)
# -------------------------------------------
# R5c step 11. Runs ONLY on a main server with a standby linked — decided by the
# link record `.standby/main.json`, which `standby-velaris.sh link` writes and
# `unlink` removes. No setting turns it on, and setup/update never write that
# file: without it, nothing changes.

# Anything already in COMPOSE_PROFILES (e.g. `observability`) is kept.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

_vcp_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_vcp_env="${VELARIS_DIR:-$(cd "$_vcp_dir/../.." && pwd)}/.env"

_vcp_setting="${VELARIS_BUNDLED_MINIO:-}"
if [ -z "$_vcp_setting" ] && [ -f "$_vcp_env" ]; then
  _vcp_setting="$(grep -E '^VELARIS_BUNDLED_MINIO=' "$_vcp_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)"
fi

_vcp_minio=false
case "$(printf '%s' "$_vcp_setting" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)  _vcp_minio=true ;;
  0|false|no|off) _vcp_minio=false ;;
  *)
    # Unset, empty or unrecognised: decide from what this host already holds.
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'velaris-minio' \
       || docker volume ls --format '{{.Name}}' 2>/dev/null | grep -qE '(^|_)velaris-minio-data$'; then
      _vcp_minio=true
    fi ;;
esac

_vcp_bus="${VELARIS_CASE_EVENT_BUS_ENABLED:-}"
if [ -z "$_vcp_bus" ] && [ -f "$_vcp_env" ]; then
  _vcp_bus="$(grep -E '^VELARIS_CASE_EVENT_BUS_ENABLED=' "$_vcp_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)"
fi
_vcp_events=false
case "$(printf '%s' "$_vcp_bus" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) _vcp_events=true ;;
esac

# Parts that run on other servers (VELARIS_EXTERNAL_SERVICES) — functions only.
# Sourced here, before the pooler test below needs velaris_is_external.
# shellcheck source=/dev/null
[ -f "$_vcp_dir/../external-services.sh" ] && . "$_vcp_dir/../external-services.sh"

_vcp_pool="${VELARIS_CASE_DATABASE_POOLER_URL:-}"
if [ -z "$_vcp_pool" ] && [ -f "$_vcp_env" ]; then
  _vcp_pool="$(grep -E '^VELARIS_CASE_DATABASE_POOLER_URL=' "$_vcp_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)"
fi
_vcp_dbkind="postgresql"
_vcp_yaml="$(dirname "$_vcp_env")/velaris.yaml"
if [ -f "$_vcp_yaml" ]; then
  # Same reading as start-velaris.sh's _yaml_val: the first `database:` key, any indent.
  _vcp_dbkind="$(grep -E '^[[:space:]]*database:' "$_vcp_yaml" 2>/dev/null | head -1 | sed -E 's/#.*//' | cut -d: -f2- | tr -d "\"' " | tr '[:upper:]' '[:lower:]' || true)"
  [ -n "$_vcp_dbkind" ] || _vcp_dbkind="postgresql"
fi
_vcp_pooler=false
if [ -n "$_vcp_pool" ] && [ "$_vcp_dbkind" = postgresql ]; then
  _vcp_pooler=true
  if type velaris_is_external >/dev/null 2>&1 && velaris_is_external database 2>/dev/null; then
    _vcp_pooler=false
  fi
fi

_vcp_standby=false
[ -f "$(dirname "$_vcp_env")/.standby/main.json" ] && _vcp_standby=true

# The shell's COMPOSE_PROFILES wins over the one in --env-file, so exporting ours
# would silently drop a profile an operator set in .env. Start from theirs.
if [ -z "${COMPOSE_PROFILES:-}" ] && [ -f "$_vcp_env" ]; then
  COMPOSE_PROFILES="$(grep -E '^COMPOSE_PROFILES=' "$_vcp_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)"
fi
if [ "$_vcp_minio" = true ]; then
  case ",${COMPOSE_PROFILES:-}," in
    *,minio,*) ;;
    *) COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}minio" ;;
  esac
fi
if [ "$_vcp_events" = true ]; then
  case ",${COMPOSE_PROFILES:-}," in
    *,events,*) ;;
    *) COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}events" ;;
  esac
fi
if [ "$_vcp_pooler" = true ]; then
  case ",${COMPOSE_PROFILES:-}," in
    *,pooler,*) ;;
    *) COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}pooler" ;;
  esac
fi
if [ "$_vcp_standby" = true ]; then
  case ",${COMPOSE_PROFILES:-}," in
    *,standby,*) ;;
    *) COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}standby" ;;
  esac
fi
# The replication gateway's ports, checked (R1): docker-compose.yml maps
# VELARIS_REPLICATION_GW_*, never the operator's raw value — a typo there made
# every compose command fail. A bad value is the default, with a line saying so.
for _vcp_rp in VELARIS_REPLICATION_PORT:5442:VELARIS_REPLICATION_GW_PORT \
               VELARIS_REPLICATION_TEMPORAL_PORT:5443:VELARIS_REPLICATION_GW_TEMPORAL_PORT; do
  _vcp_name="${_vcp_rp%%:*}"; _vcp_def="${_vcp_rp#*:}"; _vcp_def="${_vcp_def%%:*}"
  eval "_vcp_val=\${$_vcp_name:-}"
  if [ -z "$_vcp_val" ] && [ -f "$_vcp_env" ]; then
    _vcp_val="$(grep -E "^${_vcp_name}=" "$_vcp_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)"
  fi
  case "$_vcp_val" in
    ''|*[!0-9]*|0*|??????*) [ -n "$_vcp_val" ] && echo "$_vcp_name='$_vcp_val' is not a port — using $_vcp_def" >&2
                           _vcp_val="$_vcp_def" ;;
    *) [ "$_vcp_val" -le 65535 ] || { echo "$_vcp_name='$_vcp_val' is not a port — using $_vcp_def" >&2; _vcp_val="$_vcp_def"; } ;;
  esac
  export "${_vcp_rp##*:}=$_vcp_val"
done
unset _vcp_rp _vcp_name _vcp_def _vcp_val

# Exported only when there is something to say: an empty export is not "unset".
if [ -n "${COMPOSE_PROFILES:-}" ]; then export COMPOSE_PROFILES; else unset COMPOSE_PROFILES; fi
VELARIS_BUNDLED_MINIO_ACTIVE="$_vcp_minio"
VELARIS_EVENT_BROKER_ACTIVE="$_vcp_events"
VELARIS_POOLER_ACTIVE="$_vcp_pooler"
VELARIS_STANDBY_LINKED="$_vcp_standby"
unset _vcp_dir _vcp_env _vcp_setting _vcp_minio _vcp_bus _vcp_events _vcp_pool _vcp_dbkind _vcp_yaml _vcp_pooler _vcp_standby

# Retiring Redpanda (November 2026 release)
# -----------------------------------------
# Redpanda was the event broker until Apache Kafka replaced it. An install made
# before then may still hold a Redpanda container; the compose file no longer
# describes it, so compose will neither stop nor remove it, and with
# `restart: unless-stopped` it would run forever. Called by start-velaris.sh and
# update-velaris.sh before they start the stack. Removes the container and its
# image. The data volume (`…events-data`) is deliberately LEFT: removing data is
# the operator's act (`docker volume rm`, or `uninstall-velaris.sh`). Idempotent:
# on an install with no Redpanda it does nothing.
velaris_retire_redpanda() {
  local ids imgs
  ids="$(docker ps -aq --filter 'ancestor=redpandadata/redpanda' 2>/dev/null || true)"
  [ -z "$ids" ] && ids="$(docker ps -a --format '{{.ID}} {{.Image}}' 2>/dev/null | awk '$2 ~ /^redpandadata\/redpanda/ {print $1}' || true)"
  if [ -n "$ids" ]; then
    # shellcheck disable=SC2086
    docker rm -f $ids >/dev/null 2>&1 || true
    echo "  ✓ Retired the Redpanda event broker (replaced by Apache Kafka; its data volume is kept)"
  fi
  imgs="$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep '^redpandadata/redpanda:' || true)"
  # shellcheck disable=SC2086
  [ -n "$imgs" ] && docker rmi $imgs >/dev/null 2>&1 || true
  return 0
}

# A pooler that was turned off (R5d 6a)
# -------------------------------------
# `docker compose down` leaves the containers of an inactive profile running,
# so emptying VELARIS_CASE_DATABASE_POOLER_URL would leave PgBouncer up (with
# `restart: unless-stopped`, for good) while nothing uses it. Called by
# start-velaris.sh and update-velaris.sh before they start the stack. Does
# nothing while the pooler is on, or on an install that never had one.
velaris_retire_pooler() {
  local ids
  [ "${VELARIS_POOLER_ACTIVE:-false}" = true ] && return 0
  ids="$(docker ps -aq --filter 'label=com.docker.compose.service=velaris-pooler' 2>/dev/null || true)"
  if [ -n "$ids" ]; then
    # shellcheck disable=SC2086
    docker rm -f $ids >/dev/null 2>&1 || true
    echo "  ✓ Stopped the connection pooler (VELARIS_CASE_DATABASE_POOLER_URL is empty — connecting directly)"
  fi
  return 0
}

# The event broker's password
# ---------------------------
# The bundled Kafka refuses to start without VELARIS_CASE_EVENT_BUS_PASSWORD.
# setup-velaris.sh generates one for every new install. An install that turns
# the bus on later (or predates the password) gets one here, called by
# start-velaris.sh after the OpenBao render: written to .env and, where OpenBao
# holds the secrets, pushed there too — otherwise the next render would refuse
# (scripts/secrets-render.sh never drops a key .env has). Does nothing when the
# bus is off or a password is already set. Never prints the value.
_velaris_ensure_secret() {  # KEY LABEL — generate KEY in .env (+ OpenBao) if empty
  local key="$1" label="$2" dir="${VELARIS_DIR:-}" env cur pw
  [ -n "$dir" ] || return 0
  env="$dir/.env"
  [ -f "$env" ] || return 0
  cur="$(grep -E "^${key}=" "$env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)"
  [ -n "$cur" ] && return 0
  pw="$(openssl rand -hex 24)" || return 0
  sed -i "/^${key}=\$/d" "$env"
  printf '%s=%s\n' "$key" "$pw" >> "$env"
  echo "  ✓ Generated the ${label} (${key})"
  if [ -f "$dir/deploy/openbao/enabled" ] && [ -x "$dir/scripts/secrets-push.sh" ]; then
    if "$dir/scripts/secrets-push.sh" "${key}=${pw}" >/dev/null 2>&1; then
      echo "  ✓ ${label^} stored in OpenBao"
    else
      echo "  ⚠ Could not store it in OpenBao — run: ./scripts/secrets-push.sh --sync-env"
    fi
  fi
  return 0
}

# The internal service secret
# ---------------------------
# commerce-service calls case-service's /internal/v1 routes with it (R5e step
# 7). Every install gets one — setup for a new install, update and start for an
# existing one (pre-customer exception, CLAUDE.md 2026-10-19). Without it the
# internal routes refuse and checkout cannot open an order's case.
velaris_ensure_internal_secret() {
  _velaris_ensure_secret VELARIS_INTERNAL_SERVICE_SECRET "internal service secret"
}

# The gateway's optional settings (2026-11-04)
# -------------------------------------------
# Written as files the gateway includes by glob — never as template variables:
# a new variable in nginx.conf.template meets a gateway container started with
# the OLD environment, renders literally, and nginx refuses to start. A glob
# that matches nothing is simply off. Only VELARIS_GW_TRUST_CF_CONNECTING_IP so
# far: exactly "true" (behind a Cloudflare tunnel) keys rate limits on
# Cf-Connecting-Ip; anything else removes the file — off, as before.
velaris_gateway_options() {
  local dir="${VELARIS_DIR:-}" opt v
  [ -n "$dir" ] || return 0
  opt="$dir/deploy/docker-compose/gateway/options"
  mkdir -p "$opt" 2>/dev/null || return 0
  v="$(grep -E '^VELARIS_GW_TRUST_CF_CONNECTING_IP=' "$dir/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " | tr '[:upper:]' '[:lower:]' || true)"
  if [ "$v" = "true" ]; then
    printf '1 1;\n' > "$opt/cf-trust.map"
  else
    rm -f "$opt/cf-trust.map"
  fi
  return 0
}

velaris_ensure_event_bus_password() {
  [ "${VELARIS_EVENT_BROKER_ACTIVE:-false}" = true ] || return 0
  # A broker on another server has its own password; inventing one here would
  # only make the platform fail to authenticate (and the check below refuses a
  # missing one by name instead).
  if type velaris_is_external >/dev/null 2>&1 && velaris_is_external events; then return 0; fi
  _velaris_ensure_secret VELARIS_CASE_EVENT_BUS_PASSWORD "event broker password"
}

# The cache's password
# --------------------
# Valkey runs on every install (LiveKit uses it too) and has had no password.
# setup-velaris.sh gives a NEW install one and turns the cache on. When an
# operator turns VELARIS_CASE_REDIS_ENABLED on for an existing install, this
# (called by start-velaris.sh) gives it a password at the same time; Valkey,
# LiveKit and the platform all read the same VELARIS_CASE_REDIS_PASSWORD. An
# install that leaves the cache off is not touched: no password, as before.
velaris_ensure_cache_password() {
  local dir="${VELARIS_DIR:-}" on
  [ -n "$dir" ] && [ -f "$dir/.env" ] || return 0
  on="$(grep -E '^VELARIS_CASE_REDIS_ENABLED=' "$dir/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " | tr '[:upper:]' '[:lower:]' || true)"
  case "$on" in 1|true|yes|on) ;; *) return 0 ;; esac
  _velaris_ensure_secret VELARIS_CASE_REDIS_PASSWORD "cache password"
}
