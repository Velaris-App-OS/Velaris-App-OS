#!/usr/bin/env bash
# Group K — one-time OpenBao initialization for Velaris secrets.
#
# Does, in order:
#   1. operator init (1 share — Tier 1; see Tier 2 note below)
#   2. unseal
#   3. enable KV v2 at secret/
#   4. enable AppRole auth
#   5. create the read-only velaris-agent policy (secret/data/velaris/*)
#   6. mint the agent's role_id / secret_id
#
# Output files (all chmod 600, all gitignored):
#   deploy/openbao/.bao-init.json   — unseal key + root token. GUARD THIS.
#   deploy/openbao/agent/role_id    — AppRole credentials for the render agent
#   deploy/openbao/agent/secret_id
#
# Tier 2 upgrade: re-init with -key-shares=5 -key-threshold=3 or configure
# KMS auto-unseal; rotate the root token after setting up admin policies.
set -euo pipefail

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BAO="$HELIX_DIR/scripts/bao.sh"
OUT_DIR="$HELIX_DIR/deploy/openbao"
INIT_FILE="$OUT_DIR/.bao-init.json"

command -v jq >/dev/null || { echo "✗ jq is required"; exit 1; }
docker ps --format '{{.Names}}' | grep -q velaris-openbao \
  || { echo "✗ velaris-openbao container is not running"; exit 1; }

if [ -f "$INIT_FILE" ]; then
  echo "✗ $INIT_FILE already exists — OpenBao is already initialized."
  echo "  To re-initialize from scratch: docker compose down openbao &&" \
       "docker volume rm docker-compose_openbao-data && remove $INIT_FILE"
  exit 1
fi

# Fresh named volumes are root-owned; the server runs as the openbao user.
# One-time, idempotent.
docker exec -u root velaris-openbao chown -R openbao:openbao /openbao/data

echo "▶ Initializing OpenBao (1 key share — Tier 1)..."
"$BAO" operator init -key-shares=1 -key-threshold=1 -format=json > "$INIT_FILE"
chmod 600 "$INIT_FILE"

UNSEAL_KEY=$(jq -r '.unseal_keys_b64[0]' "$INIT_FILE")
ROOT_TOKEN=$(jq -r '.root_token' "$INIT_FILE")

echo "▶ Unsealing..."
"$BAO" operator unseal "$UNSEAL_KEY" >/dev/null

echo "▶ Enabling KV v2 at secret/ ..."
BAO_TOKEN="$ROOT_TOKEN" "$BAO" secrets enable -path=secret -version=2 kv >/dev/null

echo "▶ Enabling AppRole auth..."
BAO_TOKEN="$ROOT_TOKEN" "$BAO" auth enable approle >/dev/null

echo "▶ Writing read-only agent policy..."
BAO_TOKEN="$ROOT_TOKEN" "$BAO" policy write velaris-agent - >/dev/null <<'EOF'
path "secret/data/velaris/*" {
  capabilities = ["read"]
}
EOF

echo "▶ Creating velaris-agent AppRole..."
BAO_TOKEN="$ROOT_TOKEN" "$BAO" write auth/approle/role/velaris-agent \
  token_policies="velaris-agent" \
  token_ttl=5m token_max_ttl=10m \
  secret_id_num_uses=0 secret_id_ttl=0 >/dev/null

mkdir -p "$OUT_DIR/agent"
BAO_TOKEN="$ROOT_TOKEN" "$BAO" read -field=role_id auth/approle/role/velaris-agent/role-id \
  > "$OUT_DIR/agent/role_id"
BAO_TOKEN="$ROOT_TOKEN" "$BAO" write -field=secret_id -f auth/approle/role/velaris-agent/secret-id \
  > "$OUT_DIR/agent/secret_id"
chmod 600 "$OUT_DIR/agent/role_id" "$OUT_DIR/agent/secret_id"

echo ""
echo "✓ OpenBao initialized."
echo "  Unseal key + root token: $INIT_FILE (chmod 600 — do not commit, do not share)"
echo "  Next: ./scripts/secrets-push.sh --sync-env   # import your current .env"
