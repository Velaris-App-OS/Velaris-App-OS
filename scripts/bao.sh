#!/usr/bin/env bash
# Group K — thin wrapper: run any OpenBao CLI command against the platform's
# server.  Usage:  ./scripts/bao.sh status | ./scripts/bao.sh kv get secret/velaris/env
# Authenticated commands export BAO_TOKEN first (or use the root token file:
#   BAO_TOKEN=$(jq -r .root_token deploy/openbao/.bao-init.json) ./scripts/bao.sh ...)
set -euo pipefail

CONTAINER="velaris-openbao"

docker exec -i \
  -e "BAO_ADDR=http://127.0.0.1:8200" \
  -e "BAO_TOKEN=${BAO_TOKEN:-}" \
  "$CONTAINER" bao "$@"
