#!/usr/bin/env bash
# Group K — unseal OpenBao from the local Tier-1 keyfile. Safe to call when
# already unsealed (no-op). Exits 0 on unsealed, 1 on anything else; never
# prints key material.
set -euo pipefail

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INIT_FILE="$HELIX_DIR/deploy/openbao/.bao-init.json"

HEALTH=$(curl -s -m 3 -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:8300/v1/sys/health?sealedcode=472&uninitcode=471" || echo "000")
case "$HEALTH" in
  200|429) exit 0 ;;                      # already unsealed
  472) ;;                                  # sealed — proceed
  *)   echo "✗ OpenBao not unsealable (health=$HEALTH)"; exit 1 ;;
esac

[ -f "$INIT_FILE" ] || { echo "✗ keyfile $INIT_FILE missing"; exit 1; }
UNSEAL_KEY=$(jq -r '.unseal_keys_b64[0]' "$INIT_FILE" 2>/dev/null || echo "")
{ [ -n "$UNSEAL_KEY" ] && [ "$UNSEAL_KEY" != "null" ]; } \
  || { echo "✗ keyfile unreadable or malformed"; exit 1; }

"$HELIX_DIR/scripts/bao.sh" operator unseal "$UNSEAL_KEY" >/dev/null
echo "✓ OpenBao unsealed"
