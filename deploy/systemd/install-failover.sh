#!/usr/bin/env bash
# Install (or remove) the automatic-failover units on THIS server (R5c step 12).
#
#   sudo ./deploy/systemd/install-failover.sh            # install + enable
#   sudo ./deploy/systemd/install-failover.sh --remove   # disable + remove
#
# Run once on the main AND on the standby, after `standby-velaris.sh failover
# enable`. Installing does nothing by itself on a server where failover is not
# enabled: the agent and the fence check run only while
# .standby/failover/failover.json exists. Neither setup nor update runs this.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }

VELARIS_DIR="${VELARIS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VELARIS_USER="${SUDO_USER:-$(stat -c '%U' "$VELARIS_DIR")}"
SRC_DIR="$VELARIS_DIR/deploy/systemd"
UNIT_DIR="${VELARIS_UNIT_DIR:-/etc/systemd/system}"
UNITS=(velaris-failover-agent.service velaris-failover-agent.path
       velaris-failover-fence.service velaris-failover-fence.timer)

if [ "${1:-}" = "--remove" ]; then
  systemctl disable --now velaris-failover-fence.timer velaris-failover-agent.path \
    velaris-failover-agent.service 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
  systemctl daemon-reload
  echo "✓ Failover units removed."
  exit 0
fi

[ "$VELARIS_USER" != root ] || { echo "✗ Refusing to run the agent as root — run sudo from the install owner's shell." >&2; exit 1; }
# Fencing stops this server's containers.
if ! id -nG "$VELARIS_USER" | tr ' ' '\n' | grep -qx docker; then
  echo "✗ $VELARIS_USER is not in the docker group — the agent could not stop this server's databases." >&2
  exit 1
fi
command -v ssh >/dev/null || { echo "✗ ssh (OpenSSH client) is not installed." >&2; exit 1; }

for unit in "${UNITS[@]}"; do
  sed -e "s|__VELARIS_USER__|$VELARIS_USER|g" \
      -e "s|__VELARIS_DIR__|$VELARIS_DIR|g" \
      "$SRC_DIR/$unit" > "$UNIT_DIR/$unit"
  echo "  installed $UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable velaris-failover-agent.service >/dev/null
systemctl enable --now velaris-failover-agent.path
# The agent first: the fence check must not judge a lease the agent has not
# had the chance to renew yet (failover enabled long before these units).
# Ready = a main that renewed, or any other role (a standby, a server without a
# standby): the fence check never acts there.
AGENT_JSON="$VELARIS_DIR/.standby/failover/agent.json"
_agent_ready() {
  [ -f "$AGENT_JSON" ] && grep -q '"renewed": true\|"role": "standby"\|"role": "other"\|"role": "holder"' "$AGENT_JSON"
}
if [ -f "$VELARIS_DIR/.standby/failover/failover.json" ]; then
  rm -f "$AGENT_JSON"            # judge THIS start, not an older run's file
  systemctl restart velaris-failover-agent.service
  for _ in $(seq 1 20); do _agent_ready && break; sleep 1; done
  if ! _agent_ready; then
    echo "✗ The agent did not report within 20 s (a main must renew its lease at the witness) —" >&2
    echo "  the fence check is NOT armed. Check: ./standby-velaris.sh failover status" >&2
    exit 1
  fi
fi
systemctl enable --now velaris-failover-fence.timer
echo
echo "✓ Failover units installed. ./standby-velaris.sh failover status"
