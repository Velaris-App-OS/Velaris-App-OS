#!/usr/bin/env bash
# Group K — write secrets into OpenBao (secret/velaris/env, KV v2).
#
# Usage:
#   ./scripts/secrets-push.sh --sync-env          # import the whole .env (migration / after env-sync)
#   ./scripts/secrets-push.sh KEY=VALUE [K2=V2…]  # set/update individual keys (day-2 path)
#
# KV v2 "patch" semantics keep unrelated keys intact on single-key updates.
# Values are passed via stdin/files, never argv to other processes, and are
# never echoed.
set -euo pipefail

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INIT_FILE="$HELIX_DIR/deploy/openbao/.bao-init.json"
ENV_FILE="$HELIX_DIR/.env"

command -v jq >/dev/null || { echo "✗ jq is required"; exit 1; }
[ -f "$INIT_FILE" ] || { echo "✗ $INIT_FILE missing — run ./scripts/secrets-init.sh first"; exit 1; }
ROOT_TOKEN=$(jq -r '.root_token' "$INIT_FILE")

# Build a JSON object of key/value pairs to write
if [ "${1:-}" = "--sync-env" ]; then
  [ -f "$ENV_FILE" ] || { echo "✗ $ENV_FILE not found"; exit 1; }
  # .env -> JSON: skip comments/blank lines, split on first '=', strip
  # surrounding quotes (compose and pydantic both accept unquoted values)
  # Flat JSON map — the bao kv CLI @file form takes the fields directly.
  # Values are stored VERBATIM, original quoting included: the rendered .env
  # must be byte-identical to the imported one, because start-velaris.sh
  # shell-sources it (quoted PEMs with spaces) while pydantic and compose
  # parse it as dotenv. Verbatim round-trip satisfies all three consumers.
  PAYLOAD=$(awk -F= '
    /^[[:space:]]*#/ {next} /^[[:space:]]*$/ {next}
    {
      key=$1; sub(/^[[:space:]]+/,"",key); sub(/[[:space:]]+$/,"",key)
      val=substr($0, index($0,"=")+1)
      printf "%s\t%s\n", key, val
    }' "$ENV_FILE" | jq -Rn '[inputs | split("\t") | {(.[0]): (.[1:] | join("\t"))}] | add')
  COUNT=$(printf '%s' "$PAYLOAD" | jq 'length')
  echo "▶ Importing $COUNT keys from .env into secret/velaris/env ..."
  printf '%s' "$PAYLOAD" | docker exec -i \
    -e "BAO_ADDR=http://127.0.0.1:8200" -e "BAO_TOKEN=$ROOT_TOKEN" \
    velaris-openbao bao kv put secret/velaris/env @/dev/stdin >/dev/null
  echo "✓ Imported. Verify key names with:"
  echo "  BAO_TOKEN=\$(jq -r .root_token $INIT_FILE) ./scripts/bao.sh kv get -format=json secret/velaris/env | jq '.data.data | keys'"
else
  [ $# -ge 1 ] || { echo "usage: $0 --sync-env | KEY=VALUE [KEY=VALUE…]"; exit 1; }
  # Values land in .env verbatim — if a value contains spaces or shell
  # specials, include the double quotes yourself: KEY='"some value"'
  PAYLOAD=$(printf '%s\n' "$@" | jq -Rn '[inputs | split("=") | {(.[0]): (.[1:] | join("="))}] | add')
  echo "▶ Patching $# key(s) in secret/velaris/env ..."
  printf '%s' "$PAYLOAD" | docker exec -i \
    -e "BAO_ADDR=http://127.0.0.1:8200" -e "BAO_TOKEN=$ROOT_TOKEN" \
    velaris-openbao bao kv patch secret/velaris/env @/dev/stdin >/dev/null
  echo "✓ Patched. Run ./scripts/secrets-render.sh (or restart the platform) to apply."
fi
