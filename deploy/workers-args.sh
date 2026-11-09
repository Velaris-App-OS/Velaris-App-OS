# Shared by start-velaris.sh and the systemd exec wrapper (R6, 2026-10-30).
# Source it, then:  _velaris_workers_args case-service   → echoes the uvicorn flag.
#
# How many processes one service runs: VELARIS_WORKERS_<SERVICE>, e.g.
# VELARIS_WORKERS_CASE_SERVICE=4. Each service was always ONE process, so it could
# use one CPU core however big the box: R6 found case-service and user-service
# pegged at one core each while half of an 8 vCPU box sat idle.
#
# Prints nothing when the setting is unset, empty or 1 — the default — so both
# launch paths keep exactly the command line they had. A value that is not a
# whole number from 1 to 64 is ignored with a line saying so (a typo must not
# stop a service from starting). Never in development: --workers and --reload
# cannot be combined.
#
# Safe to raise because the services were built for it: background jobs run
# under leases (one copy however many processes start them), the HxVault key
# cache resyncs across processes, websockets relay through the cache. It needs
# the cache on (VELARIS_CASE_REDIS_ENABLED=true); without it, one process.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

_velaris_workers_args() {
  local service="$1"
  [ "${VELARIS_RUN_MODE:-production}" = development ] && return 0
  local var
  var="VELARIS_WORKERS_$(printf '%s' "$service" | tr 'a-z-' 'A-Z_')"
  local n="${!var:-}"
  n="${n#"${n%%[![:space:]]*}"}"; n="${n%"${n##*[![:space:]]}"}"   # trim the ends only
  [ -z "$n" ] && return 0
  case "$n" in
    *[!0-9]*|0*) echo "$var='$n' is not a whole number from 1 to 64 — ignored, one process" >&2; return 0 ;;
  esac
  if [ "$n" -gt 64 ]; then
    echo "$var=$n is more than 64 — ignored, one process" >&2
    return 0
  fi
  [ "$n" -gt 1 ] || return 0
  # Codes, attempt limits and live updates are shared between processes only
  # through the cache. Without it a sign-in code issued by one process is
  # unknown to the next, and every limit — login and portal codes included —
  # is N times looser. So more than one process needs the cache on.
  case "$(printf '%s' "${VELARIS_CASE_REDIS_ENABLED:-}" | tr 'A-Z' 'a-z')" in
    true|1|yes|on) ;;
    *) echo "$var=$n needs VELARIS_CASE_REDIS_ENABLED=true (the shared cache) — ignored, one process" >&2; return 0 ;;
  esac
  printf -- '--workers %s' "$n"
  return 0
}
