# The platform's port table, and the check that runs before anything binds them.
#
# Two problems this solves, both found on 2026-08-24.
#
# 1. **Ports were literals.** The gateway's upstreams, the launch commands and
#    the systemd units each spelled 8201-8204 out. A client already using one of
#    those had no way to move it, and nothing told them before the bind failed.
# 2. **Studio drifted instead of failing.** `vite preview` finds its port busy
#    and quietly takes the next one, so the unit says 5173, the browser needs
#    5174, and nothing anywhere reports a problem. Every other service fails
#    loudly; this one lied.
#
# So: one place names the ports, every consumer reads them from here, and both
# the installer and the updater ask "are these actually free?" BEFORE they act.
#
# Defaults are exactly the values the platform has always used, so an install
# that sets none of these keeps the ports it has. Overriding is per-install and
# lives in `.env` (or OpenBao, where that is in use) beside everything else.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

# ── The table ─────────────────────────────────────────────────────────────────
# VELARIS_CASE_SERVICE_PORT keeps its historical name: it is the GATEWAY port,
# it is already documented, and renaming a variable an install already sets is
# how you break an upgrade for no gain.
VELARIS_CASE_SERVICE_PORT="${VELARIS_CASE_SERVICE_PORT:-8200}"
VELARIS_ENGINE_PORT="${VELARIS_ENGINE_PORT:-8100}"
VELARIS_CASE_PORT="${VELARIS_CASE_PORT:-8201}"
VELARIS_ANALYTICS_PORT="${VELARIS_ANALYTICS_PORT:-8202}"
VELARIS_HXGRAPH_PORT="${VELARIS_HXGRAPH_PORT:-8203}"
VELARIS_HXMIGRATE_PORT="${VELARIS_HXMIGRATE_PORT:-8204}"
VELARIS_COMMERCE_PORT="${VELARIS_COMMERCE_PORT:-8207}"   # R5e step 7
VELARIS_PORTAL_PORT="${VELARIS_PORTAL_PORT:-8205}"       # R5e step 8
VELARIS_USER_PORT="${VELARIS_USER_PORT:-8206}"           # R5e step 10
VELARIS_WORKER_PORT="${VELARIS_WORKER_PORT:-8208}"       # R5e step 9 (health only)
VELARIS_STUDIO_PORT="${VELARIS_STUDIO_PORT:-5173}"

# name:variable — the order they are reported in.
VELARIS_PORT_TABLE="\
gateway:VELARIS_CASE_SERVICE_PORT
engine:VELARIS_ENGINE_PORT
case-service:VELARIS_CASE_PORT
analytics-service:VELARIS_ANALYTICS_PORT
hxgraph-service:VELARIS_HXGRAPH_PORT
hxmigrate-service:VELARIS_HXMIGRATE_PORT
commerce-service:VELARIS_COMMERCE_PORT
portal-service:VELARIS_PORTAL_PORT
user-service:VELARIS_USER_PORT
worker-service:VELARIS_WORKER_PORT
studio:VELARIS_STUDIO_PORT"

# ── Who holds a port ──────────────────────────────────────────────────────────
#
# Three answers, not two. "In use" is not the same question as "in use by
# something that is not us": an update runs while the platform is up, so its own
# ports are supposed to be busy, and an installer re-run finds its own last
# attempt. Answering only free/busy would make the updater refuse every update.
#
# When ownership cannot be determined we say `unknown`, never `free` — guessing
# free is the answer that ends in a half-installed platform.
#
#   _velaris_port_owner 8201 → free | velaris | unknown | foreign:<name>
_velaris_port_owner() {
  local port="$1"

  command -v ss >/dev/null 2>&1 || { echo "unknown"; return; }
  ss -ltn 2>/dev/null | grep -qE "[:.]${port}[[:space:]]" || { echo "free"; return; }

  # A velaris container publishing it (the gateway is host-networked nginx, so
  # the host sees a process it cannot attribute — ask docker instead).
  if command -v docker >/dev/null 2>&1; then
    if docker ps --filter "name=velaris-" --format '{{.Names}} {{.Ports}}' 2>/dev/null \
         | grep -qE "[:.]${port}->|velaris-gateway"; then
      # velaris-gateway runs host-networked, so its ports do not show in `Ports`.
      if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^velaris-gateway$' \
         && [ "$port" = "${VELARIS_CASE_SERVICE_PORT}" ]; then
        echo "velaris"; return
      fi
    fi
  fi

  local pid
  pid=$(ss -ltnp 2>/dev/null | grep -E "[:.]${port}[[:space:]]" \
        | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  if [ -z "$pid" ]; then
    # The kernel hides another process's sockets from `ss -p` unless uid AND gid
    # match — and setup re-runs itself under `sg docker`, so every one of our own
    # units read "unknown" and the update agent was refused (2026-10-20). The
    # socket's cgroup needs no such access; an `ss` without --cgroup prints
    # nothing here and we fall through to "unknown", as before.
    if ss -ltnH --cgroup 2>/dev/null | grep -E "[:.]${port}[[:space:]]" \
         | grep -qE 'cgroup:[^ ]*/velaris-[^ /]*\.service'; then
      echo "velaris"; return
    fi
    echo "unknown"; return
  fi

  # Started by one of our units?
  if grep -qs 'velaris-' "/proc/$pid/cgroup" 2>/dev/null; then echo "velaris"; return; fi
  # Or started from the install directory by hand (nohup, a dev shell).
  if [ -n "${VELARIS_DIR:-}" ] \
     && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "$VELARIS_DIR"; then
    echo "velaris"; return
  fi

  local comm; comm=$(cat "/proc/$pid/comm" 2>/dev/null || echo "pid $pid")
  echo "foreign:${comm}"
}

# ── The preflight ─────────────────────────────────────────────────────────────
#
#   _velaris_ports_preflight          → report; 0 if every port is usable
#   _velaris_ports_preflight --quiet  → same, no output on success
#
# "Usable" means free, or already held by us. A port held by something else is
# the only failure, and it is named along with what holds it — an operator who
# is told "8202 is taken" and not "by pgbouncer" has been given a puzzle.
_velaris_ports_preflight() {
  local quiet=0; [ "${1:-}" = "--quiet" ] && quiet=1
  local blocked=0 line name var port owner

  while IFS= read -r line; do
    name="${line%%:*}"; var="${line##*:}"
    eval "port=\${$var}"
    owner=$(_velaris_port_owner "$port")
    case "$owner" in
      free)     [ "$quiet" = 1 ] || printf '  %-20s %-6s free\n'  "$name" "$port" ;;
      velaris)  [ "$quiet" = 1 ] || printf '  %-20s %-6s in use by Velaris (fine)\n' "$name" "$port" ;;
      unknown)  printf '  %-20s %-6s IN USE — owner could not be determined\n' "$name" "$port" >&2
                blocked=$((blocked + 1)) ;;
      foreign:*) printf '  %-20s %-6s IN USE by %s — set %s to a free port\n' \
                   "$name" "$port" "${owner#foreign:}" "$var" >&2
                blocked=$((blocked + 1)) ;;
    esac
  done <<VELARIS_PORTS_EOF
$VELARIS_PORT_TABLE
VELARIS_PORTS_EOF

  [ "$blocked" = 0 ]
}
