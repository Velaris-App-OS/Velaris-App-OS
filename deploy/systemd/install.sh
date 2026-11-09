#!/usr/bin/env bash
# Install the Velaris services as systemd units.
#
# Run on the SERVER, from the Velaris directory:
#   sudo ./deploy/systemd/install.sh
#
# Replaces the nohup-from-a-shell model in start-velaris.sh with supervised
# services: they survive logout, restart on crash, start on boot, and log to
# journald instead of a /tmp file that the next start truncates.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

UNIT_DIR=/etc/systemd/system
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VELARIS_DIR="$(cd "$SRC_DIR/../.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo — installing to $UNIT_DIR"; exit 1; }

# The units must run as the human who owns the checkout and the toolchains, not
# as root: uv and npm live in that user's home, and root-owned build artefacts
# in the repo would break the next non-root run.
VELARIS_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
[ "$VELARIS_USER" != "root" ] || { echo "Refusing to run Velaris as root. Use: sudo -u <user> ... or run sudo from that user's shell."; exit 1; }
VELARIS_HOME="$(getent passwd "$VELARIS_USER" | cut -d: -f6)"

# Absolute paths — systemd runs with a minimal PATH and no shell profile, so
# `uv` and `npm` from a version manager would simply not be found.
UV="$(sudo -u "$VELARIS_USER" bash -lc 'command -v uv' 2>/dev/null || true)"
NPM="$(sudo -u "$VELARIS_USER" bash -lc 'command -v npm' 2>/dev/null || true)"
[ -n "$UV" ]  || { echo "uv not found for user $VELARIS_USER"; exit 1; }
[ -n "$NPM" ] || { echo "npm not found for user $VELARIS_USER"; exit 1; }
[ -f "$VELARIS_DIR/.env" ] || { echo "No .env at $VELARIS_DIR/.env — the units read it as EnvironmentFile"; exit 1; }
# velaris.yaml carries DATABASE_BACKEND, which is NOT in .env and whose absence
# silently defaults to postgresql — wrong on a MySQL/MariaDB install.
[ -f "$VELARIS_DIR/velaris.yaml" ] || { echo "No velaris.yaml at $VELARIS_DIR — velaris-exec.sh derives DATABASE_BACKEND from it"; exit 1; }
chmod +x "$SRC_DIR/velaris-exec.sh"
# ExecStartPre= for the engine and case-service units: blocks until the DB port
# is open so neither service can win the race against a still-booting Postgres.
[ -f "$SRC_DIR/velaris-wait-deps.sh" ] || { echo "Missing $SRC_DIR/velaris-wait-deps.sh — the units reference it as ExecStartPre"; exit 1; }
chmod +x "$SRC_DIR/velaris-wait-deps.sh"

echo "user=$VELARIS_USER  dir=$VELARIS_DIR"
echo "uv=$UV  npm=$NPM"
echo "database=$(grep -E '^\s+database:' "$VELARIS_DIR/velaris.yaml" | head -1 | sed -E 's/.*database:[[:space:]]*//')"
echo

for unit in velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service velaris-commerce-service velaris-portal-service velaris-user-service velaris-worker-service velaris-studio; do
  sed -e "s|__VELARIS_USER__|$VELARIS_USER|g" \
      -e "s|__VELARIS_DIR__|$VELARIS_DIR|g" \
      -e "s|__VELARIS_HOME__|$VELARIS_HOME|g" \
      -e "s|__UV__|$UV|g" \
      -e "s|__NPM__|$NPM|g" \
      -e "s|__EXEC__|$SRC_DIR/velaris-exec.sh|g" \
      -e "s|__WAITDEPS__|$SRC_DIR/velaris-wait-deps.sh|g" \
      "$SRC_DIR/$unit.service" > "$UNIT_DIR/$unit.service"
  echo "  installed $UNIT_DIR/$unit.service"
done

# The units Require= docker.service, and every backing service (database, cache,
# broker, vault) is a container carrying `restart: unless-stopped`. If Docker
# itself is not enabled at boot, none of that fires and the platform comes back
# from a reboot with no database — the 2026-08-05 outage.
if ! systemctl is-enabled --quiet docker 2>/dev/null; then
  echo "  docker.service was not enabled at boot — enabling it"
  systemctl enable docker
fi

systemctl daemon-reload
# Until this run, case-service's unit may have been running commerce-service,
# portal-service and user-service beside it (velaris-exec.sh does that while their units are
# missing — an update cannot install units). Now that the units exist, restart
# case-service once so it stops doing so and the ports (8207, 8205, 8206) are free for
# the real units — then start them, or a service that was running before this
# is down after it (found 2026-10-20: checkout, payments and marketplace 502
# until a manual start).
_handover=()
for _pair in "commerce_service:velaris-commerce-service" "portal_service:velaris-portal-service" \
             "user_service:velaris-user-service"; do
  if pgrep -f "uvicorn ${_pair%%:*}.main:app" >/dev/null 2>&1 \
     && ! systemctl is-active --quiet "${_pair#*:}" 2>/dev/null; then
    _handover+=("${_pair#*:}")
  fi
done
if systemctl is-active --quiet velaris-case-service 2>/dev/null && [ "${#_handover[@]}" -gt 0 ]; then
  echo "  case-service was running ${_handover[*]} beside it — restarting it once"
  systemctl restart velaris-case-service
  for _u in "${_handover[@]}"; do
    echo "  starting $_u in its place"
    systemctl start "$_u"
  done
fi
systemctl enable velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service velaris-commerce-service velaris-portal-service velaris-user-service velaris-worker-service velaris-studio
echo
echo "Installed and enabled (start on boot). Now:"
echo "  ./stop-velaris.sh                                   # clear the nohup'd processes first"
echo "  sudo systemctl start velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service velaris-commerce-service velaris-portal-service velaris-user-service velaris-worker-service velaris-studio"
echo "  systemctl status velaris-studio"
echo "  journalctl -u velaris-studio -f"
echo
echo "NOTE: start-velaris.sh still launches its own nohup copies. Use systemctl"
echo "      from now on, or you will end up with two of each fighting for a port."
