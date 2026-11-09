#!/usr/bin/env bash
# Velaris — which heavy parts run on OTHER servers, and the rules they must meet.
#
# SOURCE this; it defines functions only and changes nothing on its own:
#
#   . "$VELARIS_DIR/deploy/external-services.sh"
#
# The setting
# -----------
#   VELARIS_EXTERNAL_SERVICES=database,temporal,ai,meetings,events   (any subset)
#
# Unset or empty = every part bundled on this box, exactly as before. It is an
# explicit list on purpose, never inferred from the connection URLs: a URL that
# holds this box's own LAN address would otherwise silently drop the bundled
# database. Read from the environment first, then .env.
#
#   part       bundled containers it replaces        the client setting that points away
#   database   velaris-db                            VELARIS_CASE_DATABASE_URL
#   temporal   temporal, temporal-db, temporal-ui    VELARIS_CASE_TEMPORAL_HOST
#   ai         ollama                                VELARIS_CASE_AI_OLLAMA_URL
#   meetings   livekit, livekit-egress               VELARIS_CASE_LIVEKIT_URL
#   events     events (Kafka)                        VELARIS_CASE_EVENT_BUS_BROKERS
#
# MinIO keeps its own switch (VELARIS_BUNDLED_MINIO, compose-profiles.sh); the
# cache stays bundled (LiveKit dispatches through it).
#
# A bundled container whose part is made external is stopped and removed; its
# data volume is KEPT — that volume is the way back (remove the part from the
# list and start again).
#
# Refused, not warned
# -------------------
# A part on another server is reached over a network this box does not own, so
# it must be encrypted AND authenticated. `velaris_external_check` names every
# missing setting and returns 1; the start, update and systemd paths stop on it.
#
#   database (PostgreSQL)  URL ?sslmode=verify-full, or verify-ca/require WITH
#                          sslrootcert=<CA file>
#   database (MySQL/Maria) URL ?ssl_ca=<CA file>
#   temporal               VELARIS_CASE_TEMPORAL_TLS_CA, _CERT, _KEY (mutual TLS)
#   events                 VELARIS_CASE_EVENT_BUS_TLS_CA + _PASSWORD (SASL_SSL)
#   ai                     VELARIS_CASE_AI_OLLAMA_URL https:// + _AI_OLLAMA_API_KEY
#                          (Ollama has no authentication; put it behind a proxy
#                          that checks the bearer key); _AI_OLLAMA_TLS_CA when the
#                          proxy's certificate is not publicly trusted. No Ollama
#                          URL set = the platform uses no Ollama, nothing to check.
#   meetings               VELARIS_CASE_LIVEKIT_URL wss:// (and _HTTP_URL https://
#                          when set)
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

VELARIS_EXTERNAL_PARTS="database temporal ai meetings events"
# psql/pg_dump for a database on another server. Pinned like every other image
# (scripts/check-image-versions.sh scans this file); VELARIS_PG_CLIENT_IMAGE (env
# or .env) overrides it for a server newer than PostgreSQL 16.
_VX_PG_CLIENT_DEFAULT="postgres:16.15-alpine"

_vx_env_file() { printf '%s/.env' "${VELARIS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; }

# KEY → its value: the environment when set and non-empty, else the last line in .env.
_vx_get() {
  local key="$1" v="${!1:-}" f
  if [ -z "$v" ]; then
    f="$(_vx_env_file)"
    [ -f "$f" ] && v="$(grep -E "^${key}=" "$f" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  fi
  printf '%s' "$v"
}

# The normalised list, one space-separated line ("" when unset). An unknown word
# is an error, not ignored: a typo would otherwise leave the bundled container
# running and the platform pointed somewhere else, or the reverse.
velaris_external_list() {
  local raw w out=""
  raw="$(_vx_get VELARIS_EXTERNAL_SERVICES | tr ',' ' ' | tr '[:upper:]' '[:lower:]')"
  for w in $raw; do
    case " $VELARIS_EXTERNAL_PARTS " in
      *" $w "*) case " $out " in *" $w "*) ;; *) out="${out:+$out }$w" ;; esac ;;
      *) echo "VELARIS_EXTERNAL_SERVICES: unknown part '$w' (allowed: ${VELARIS_EXTERNAL_PARTS// /, })" >&2
         return 1 ;;
    esac
  done
  printf '%s' "$out"
}

velaris_is_external() {  # PART
  case " $(velaris_external_list 2>/dev/null) " in *" $1 "*) return 0 ;; esac
  return 1
}

# Compose services a part replaces.
_vx_services_of() {
  case "$1" in
    database) echo "velaris-db velaris-pooler" ;;
    temporal) echo "temporal temporal-db temporal-ui" ;;
    ai)       echo "ollama" ;;
    meetings) echo "livekit livekit-egress" ;;
    events)   echo "events" ;;
  esac
}

velaris_external_compose_services() {
  local p out=""
  for p in $(velaris_external_list 2>/dev/null); do out="${out:+$out }$(_vx_services_of "$p")"; done
  printf '%s' "$out"
}

# The services `docker compose up -d` should start. EMPTY when nothing is
# external — the caller then runs a plain `up -d`, byte-for-byte the old call.
# Needs COMPOSE_PROFILES already exported (compose-profiles.sh) and the compose
# file + env file as arguments.
velaris_compose_up_services() {  # COMPOSE_FILE ENV_FILE
  local skip svc all out=""
  skip=" $(velaris_external_compose_services) "
  [ "$skip" = "  " ] && return 0
  # A list we cannot read is an error, never "start everything": that would
  # bring back the very containers this install moved elsewhere.
  if ! all="$(docker compose -f "$1" --env-file "$2" config --services)"; then
    echo "  ✗ docker compose config failed (above) — cannot tell which containers to start" >&2
    return 1
  fi
  for svc in $all; do
    case "$skip" in *" $svc "*) ;; *) out="${out:+$out }$svc" ;; esac
  done
  printf '%s' "$out"
}

# Stop and remove the bundled containers of external parts. Volumes are kept.
velaris_retire_external_containers() {  # COMPOSE_FILE ENV_FILE
  local svcs svc cid stopped=""
  svcs="$(velaris_external_compose_services)"
  [ -n "$svcs" ] || return 0
  for svc in $svcs; do
    cid="$(docker compose -f "$1" --env-file "$2" ps -aq "$svc" 2>/dev/null || true)"
    [ -n "$cid" ] || continue
    docker compose -f "$1" --env-file "$2" rm -sf "$svc" >/dev/null 2>&1 || true
    stopped="${stopped:+$stopped, }$svc"
  done
  [ -n "$stopped" ] && echo "  ✓ Bundled ${stopped} stopped — running on another server now (data volumes kept)"
  return 0
}

# ── Security rules ────────────────────────────────────────────────────────────

_vx_query() {  # URL KEY → value of ?KEY= (last wins), "" if absent
  local q="${1#*\?}"
  [ "$q" = "$1" ] && return 0
  printf '%s' "$q" | tr '&' '\n' | grep -E "^$2=" | tail -1 | cut -d= -f2- || true
}

_vx_file_ok() {  # PATH LABEL → message when unusable
  if [ -z "$1" ]; then echo "$2 is not set"
  elif [ ! -r "$1" ]; then echo "$2 '$1' is not a readable file"
  fi
}

velaris_external_check() {
  local parts p url mode ca msg errs=() backend k
  parts="$(velaris_external_list)" || return 1
  [ -n "$parts" ] || return 0
  for p in $parts; do
    case "$p" in
      database)
        url="$(_vx_get VELARIS_CASE_DATABASE_URL)"
        backend="$(_vx_get VELARIS_CASE_DATABASE_BACKEND)"
        [ -n "$backend" ] || backend="${DATABASE_BACKEND:-postgresql}"
        # The scripts `source` .env: an unquoted '&' there splits the line into
        # background commands and the URL never reaches the shell (it would
        # still reach the platform through pydantic — two different URLs).
        if grep -qE '^VELARIS_CASE_DATABASE_URL=[^"'"'"'].*&' "$(_vx_env_file)" 2>/dev/null; then
          errs+=("database: VELARIS_CASE_DATABASE_URL in .env holds '&' unquoted — write it as VELARIS_CASE_DATABASE_URL=\"…\"")
        fi
        if [ -z "$url" ]; then
          errs+=("database: VELARIS_CASE_DATABASE_URL is not set")
        elif [[ "$url" == mysql* || "$url" == mariadb* || "$backend" == mysql || "$backend" == mariadb ]]; then
          ca="$(_vx_query "$url" ssl_ca)"
          msg="$(_vx_file_ok "$ca" "ssl_ca in VELARIS_CASE_DATABASE_URL")"
          [ -n "$msg" ] && errs+=("database: $msg (MySQL/MariaDB on another server needs ?ssl_ca=<CA file>)")
          case "$(_vx_query "$url" ssl_check_hostname | tr '[:upper:]' '[:lower:]')" in
            0|false|no|off) errs+=("database: ssl_check_hostname=false — the server's name must be checked") ;;
          esac
        else
          mode="$(_vx_query "$url" sslmode)"; [ -n "$mode" ] || mode="$(_vx_query "$url" ssl)"
          ca="$(_vx_query "$url" sslrootcert)"
          case "$mode" in
            verify-full) [ -n "$ca" ] && [ "$ca" != system ] && msg="$(_vx_file_ok "$ca" sslrootcert)" && [ -n "$msg" ] && errs+=("database: $msg") ;;
            verify-ca|require)
              msg="$(_vx_file_ok "$ca" sslrootcert)"
              [ -n "$msg" ] && errs+=("database: sslmode=$mode needs sslrootcert=<CA file> ($msg)") ;;
            *) errs+=("database: VELARIS_CASE_DATABASE_URL needs ?sslmode=verify-full (or verify-ca/require with sslrootcert=<CA file>); found '${mode:-none}'") ;;
          esac
        fi ;;
      temporal)
        for k in CA CERT KEY; do
          msg="$(_vx_file_ok "$(_vx_get "VELARIS_CASE_TEMPORAL_TLS_$k")" "VELARIS_CASE_TEMPORAL_TLS_$k")"
          [ -n "$msg" ] && errs+=("temporal: $msg (Temporal on another server needs mutual TLS)")
        done ;;
      events)
        msg="$(_vx_file_ok "$(_vx_get VELARIS_CASE_EVENT_BUS_TLS_CA)" VELARIS_CASE_EVENT_BUS_TLS_CA)"
        [ -n "$msg" ] && errs+=("events: $msg (a broker on another server needs SASL_SSL)")
        [ -n "$(_vx_get VELARIS_CASE_EVENT_BUS_PASSWORD)" ] || errs+=("events: VELARIS_CASE_EVENT_BUS_PASSWORD is not set") ;;
      ai)
        url="$(_vx_get VELARIS_CASE_AI_OLLAMA_URL)"
        if [ -n "$url" ]; then
          [[ "$url" == https://* ]] || errs+=("ai: VELARIS_CASE_AI_OLLAMA_URL must be https:// (found '${url%%://*}://')")
          [ -n "$(_vx_get VELARIS_CASE_AI_OLLAMA_API_KEY)" ] || errs+=("ai: VELARIS_CASE_AI_OLLAMA_API_KEY is not set (Ollama has no authentication; a proxy in front of it checks this key)")
          ca="$(_vx_get VELARIS_CASE_AI_OLLAMA_TLS_CA)"
          [ -z "$ca" ] || { msg="$(_vx_file_ok "$ca" VELARIS_CASE_AI_OLLAMA_TLS_CA)"; [ -z "$msg" ] || errs+=("ai: $msg"); }
        fi ;;
      meetings)
        url="$(_vx_get VELARIS_CASE_LIVEKIT_URL)"
        [[ "$url" == wss://* ]] || errs+=("meetings: VELARIS_CASE_LIVEKIT_URL must be wss:// (found '${url:-unset}')")
        url="$(_vx_get VELARIS_CASE_LIVEKIT_HTTP_URL)"
        [ -z "$url" ] || [[ "$url" == https://* ]] || errs+=("meetings: VELARIS_CASE_LIVEKIT_HTTP_URL must be https://") ;;
    esac
  done
  [ "${#errs[@]}" -eq 0 ] && return 0
  echo "REFUSED: parts on another server must be encrypted and authenticated (VELARIS_EXTERNAL_SERVICES=${parts// /,}):" >&2
  for msg in "${errs[@]}"; do echo "  ✗ $msg" >&2; done
  echo "  See docs/deployment-guide.md — \"Running parts on other servers\"." >&2
  return 1
}

# ── Talking to the database ───────────────────────────────────────────────────
# Bundled: `docker exec` into the container, exactly the call the scripts made
# before. External: a pinned client container on the host network, with the
# libpq URL passed in the environment (never on a command line `ps` can read)
# and the URL's certificate files mounted read-only at the same paths.
# Callers set DB_CONTAINER, DB_USER, DB_NAME as before.

# postgresql+asyncpg://…?ssl=verify-full  →  postgresql://…?sslmode=verify-full
# Only options libpq knows are kept: the URL may also carry driver options
# (asyncpg's prepared_statement_cache_size, …) that psql would refuse outright.
# `verify-full` with no CA file means a publicly trusted certificate — the
# platform then checks against the system's CAs, and libpq must be told to
# do the same (sslrootcert=system), or it looks for ~/.postgresql/root.crt.
_VX_LIBPQ_KEYS=" sslmode sslrootcert sslcert sslkey sslcrl sslsni application_name connect_timeout options target_session_attrs "
velaris_pg_url() {
  local url base kv k keep="" mode="" root="" kvs=()
  url="$(_vx_get VELARIS_CASE_DATABASE_URL)"
  url="$(printf '%s' "$url" | sed -E 's#^(postgres(ql)?)\+[a-z0-9_]+://#\1://#')"
  base="${url%%\?*}"
  if [ "$base" != "$url" ]; then
    IFS='&' read -r -a kvs <<<"${url#*\?}"      # split on & only — no globbing
    for kv in "${kvs[@]}"; do
      k="${kv%%=*}"; [ "$k" = ssl ] && { k=sslmode; kv="sslmode=${kv#*=}"; }
      case "$_VX_LIBPQ_KEYS" in *" $k "*) keep="${keep:+$keep&}$kv" ;; esac
      [ "$k" = sslmode ] && mode="${kv#*=}"
      [ "$k" = sslrootcert ] && root="${kv#*=}"
    done
  fi
  [ "$mode" = verify-full ] && [ -z "$root" ] && keep="${keep:+$keep&}sslrootcert=system"
  printf '%s' "$base${keep:+?$keep}"
}

_vx_pg_mounts() {
  local url k f
  url="$(velaris_pg_url)"
  for k in sslrootcert sslcert sslkey; do
    f="$(_vx_query "$url" "$k")"
    [ -n "$f" ] && [ "$f" != system ] && printf -- '-v\n%s:%s:ro\n' "$f" "$f"
  done
  return 0
}

_vx_pg_client_image() { local i; i="$(_vx_get VELARIS_PG_CLIENT_IMAGE)"; printf '%s' "${i:-$_VX_PG_CLIENT_DEFAULT}"; }

# The password is taken OUT of the URL and handed over as PGPASSWORD: inside the
# container the URL becomes psql's own argument, and a container's command line
# is visible to `ps` on the host. The URL left behind holds no secret.
_vx_pg_client() {  # TOOL ARGS... — run a libpq tool against the external URL
  local tool="$1"; shift
  local mounts=() url rest auth hostpart="" userinfo="" pass=""
  mapfile -t mounts < <(_vx_pg_mounts)
  url="$(velaris_pg_url)"
  # user:password@host — split on the LAST '@' of the authority: a password may
  # hold a raw '@', and anchoring on the first one reads the wrong host.
  rest="${url#*://}"; auth="${rest%%/*}"; auth="${auth%%\?*}"
  if [ "${auth%@*}" != "$auth" ]; then
    userinfo="${auth%@*}"; hostpart="${rest#"$userinfo"@}"
  fi
  if [ -n "$userinfo" ] && [ "${userinfo#*:}" != "$userinfo" ]; then
    pass="${userinfo#*:}"
    url="${url%%://*}://${userinfo%%:*}@${hostpart}"
    pass="${pass//\\/\\\\}"                         # a literal backslash stays literal
    pass="$(printf '%b' "${pass//%/\\x}")"          # percent-decoded, as libpq would
  fi
  VELARIS_PGURL="$url" PGPASSWORD="$pass" docker run --rm -i --network host \
    -e VELARIS_PGURL -e PGPASSWORD "${mounts[@]}" "$(_vx_pg_client_image)" \
    sh -c 'tool="$1"; shift; exec "$tool" -d "$VELARIS_PGURL" "$@"' sh "$tool" "$@"
}

velaris_psql() {
  if velaris_is_external database; then _vx_pg_client psql "$@"
  else docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" "$@"; fi
}

velaris_pg_dump() {
  if velaris_is_external database; then _vx_pg_client pg_dump "$@"
  else docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" "$@"; fi
}

velaris_pg_isready() {
  if velaris_is_external database; then _vx_pg_client pg_isready -q </dev/null
  # -h 127.0.0.1: over TCP, not the socket. On a first start the image runs a
  # socket-only init server, then restarts; the socket said "ready" during init
  # and migrations hit "the database system is shutting down" (2026-10-18).
  else docker exec "$DB_CONTAINER" pg_isready -h 127.0.0.1 -U "$DB_USER" -q; fi
}

# host:port Temporal answers on — TEMPORAL_HOST first (what the engine and the
# Helm charts set), then VELARIS_CASE_TEMPORAL_HOST, then the bundled default.
velaris_temporal_target() {
  local t
  t="$(_vx_get TEMPORAL_HOST)"; [ -n "$t" ] || t="$(_vx_get VELARIS_CASE_TEMPORAL_HOST)"
  printf '%s' "${t:-127.0.0.1:7233}"
}

# MySQL / MariaDB: TLS when the URL names a CA (?ssl_ca=…). Prints nothing
# otherwise, so the client calls stay what they were. Docker mount arguments
# (before the image) and client flags (after it) — one per line, for mapfile.
velaris_mysql_tls_mounts() {
  local ca
  ca="$(_vx_query "$(_vx_get VELARIS_CASE_DATABASE_URL)" ssl_ca)"
  [ -n "$ca" ] && printf -- '-v\n%s:%s:ro\n' "$ca" "$ca"
  return 0
}
# MySQL / MariaDB client calls for the scripts that are not start-velaris.sh
# (update-velaris.sh, refresh-after-update.sh). Same resolution start uses:
# velaris.yaml db_host/db_port/db_name/db_user, the password from
# VELARIS_DB_PASSWORD (never on a command line: set in docker's environment and
# passed by NAME, `-e MYSQL_PWD` — a `-e MYSQL_PWD=value` argument is readable by
# every local user in the process list), TLS from the URL's
# ssl_ca. A throwaway mysql:8 client container, which speaks to MySQL 8 and
# MariaDB 10.6+/11 alike; no client on the host needed.
_vx_yaml() {  # key default
  local v
  v="$(grep -E "^[[:space:]]+$1:" "${VELARIS_DIR:-.}/velaris.yaml" 2>/dev/null | head -1 \
       | sed -E "s/.*$1:[[:space:]]*[\"']?([^\"'#]*)[\"']?.*/\1/" | xargs 2>/dev/null || true)"
  printf '%s' "${v:-$2}"
}
_vx_mysql_run() {  # tool args…
  local tool="$1"; shift
  local -a mounts flags
  mapfile -t mounts < <(velaris_mysql_tls_mounts)
  mapfile -t flags < <(velaris_mysql_tls_flags)
  local -a timeout=()
  [ "$tool" = mysql ] && timeout=(--connect-timeout=10)   # mysqldump has no such option
  MYSQL_PWD="$(_vx_get VELARIS_DB_PASSWORD)" docker run --rm -i --network host -e MYSQL_PWD "${mounts[@]}" mysql:8.4.11 \
    "$tool" "${timeout[@]}" -h "$(_vx_yaml db_host 127.0.0.1)" -P "$(_vx_yaml db_port 3306)" \
    -u "$(_vx_yaml db_user velaris)" "${flags[@]}" "$@"
}
velaris_mysql()     { _vx_mysql_run mysql "$(_vx_yaml db_name velaris)" "$@"; }
# --single-transaction: a consistent dump of InnoDB without locking the tables
# the running platform is using; --no-tablespaces: needs no PROCESS privilege,
# which an application user on a managed server usually lacks;
# --column-statistics=0: the 8.x client otherwise queries a table MariaDB lacks.
velaris_mysqldump() { _vx_mysql_run mysqldump --single-transaction --routines --triggers --no-tablespaces --column-statistics=0 \
                        "$@" "$(_vx_yaml db_name velaris)"; }

velaris_mysql_tls_flags() {  # [mariadb] — the mariadb client spells it differently
  local ca
  ca="$(_vx_query "$(_vx_get VELARIS_CASE_DATABASE_URL)" ssl_ca)"
  [ -n "$ca" ] || return 0
  if [ "${1:-}" = mariadb ]; then printf -- '--ssl\n--ssl-ca=%s\n--ssl-verify-server-cert\n' "$ca"
  else printf -- '--ssl-mode=VERIFY_IDENTITY\n--ssl-ca=%s\n' "$ca"; fi
}
