# Shared by start-velaris.sh and the systemd exec wrapper (R1, 2026-10-30).
# Source it, then:  _velaris_port VELARIS_CASE_PORT 8201   → echoes the port to use.
#
# A service's port comes from .env. Empty already fell back to the default in
# start-velaris.sh, but under systemd `VELARIS_CASE_PORT=` in .env overrides the
# unit's default and uvicorn got `--port ''`; a typo (`820l`) stopped the
# service on both paths. Now an empty or mistyped port is the default, with one
# line naming the variable — a good value is used exactly as written.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

_velaris_port_ok() {
  case "$1" in ''|*[!0-9]*|0*) return 1 ;; esac
  [ "${#1}" -le 5 ] && [ "$1" -le 65535 ]
}

_velaris_port() {   # <variable name> <default>
  local v="${!1:-}"
  v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
  if _velaris_port_ok "$v"; then printf '%s' "$v"; return 0; fi
  [ -n "$v" ] && echo "$1='$v' is not a port — using $2" >&2
  printf '%s' "$2"
}
