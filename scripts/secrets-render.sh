#!/usr/bin/env bash
# Group K — render .env from OpenBao (one-shot agent, AppRole auth).
#
# Fail-closed by design: if OpenBao is sealed, unreachable, or the render
# comes back empty/implausible, the existing .env is left untouched and we
# exit non-zero. The platform then starts on the last-known-good secrets.
#
#   ./scripts/secrets-render.sh           # render to .env
#   ./scripts/secrets-render.sh --check   # render to stdout key names only (no write)
set -euo pipefail

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$HELIX_DIR/deploy/openbao/agent"
OUT_DIR="$AGENT_DIR/out"
RENDERED="$OUT_DIR/.env"
TARGET="$HELIX_DIR/.env"

mkdir -p "$OUT_DIR"
rm -f "$RENDERED"

# 1. Server must be up AND unsealed (health 200/429 = active/standby unsealed)
HEALTH=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8350/v1/sys/health" || echo "000")
case "$HEALTH" in
  200|429) ;;
  503) echo "✗ OpenBao is SEALED — unseal first. .env left untouched."; exit 1 ;;
  501) echo "✗ OpenBao not initialized — run ./scripts/secrets-init.sh. .env left untouched."; exit 1 ;;
  *)   echo "✗ OpenBao unreachable on :8350 (health=$HEALTH). .env left untouched."; exit 1 ;;
esac

# 2. One-shot agent render — throwaway container in the server's netns,
#    running as the invoking user so the bind-mounted output is writable
docker run --rm \
  --network "container:velaris-openbao" \
  --user "$(id -u):$(id -g)" \
  -v "$AGENT_DIR:/openbao/agent" \
  openbao/openbao:2 bao agent -config=/openbao/agent/agent.hcl >/dev/null

# 3. Sanity gates before touching .env
[ -s "$RENDERED" ] || { echo "✗ render produced no output — .env left untouched"; exit 1; }
grep -q '^HELIX_CASE_DATABASE_URL=' "$RENDERED" \
  || { echo "✗ rendered file missing anchor key HELIX_CASE_DATABASE_URL — .env left untouched"; exit 1; }

if [ "${1:-}" = "--check" ]; then
  echo "✓ render OK — keys:"
  grep -oE '^[A-Z_0-9]+' "$RENDERED" | sed 's/^/    /'
  rm -f "$RENDERED"
  exit 0
fi

# 4. First render keeps a one-time backup of the hand-maintained file
if [ -f "$TARGET" ] && [ ! -f "$HELIX_DIR/.env.pre-openbao" ]; then
  cp -p "$TARGET" "$HELIX_DIR/.env.pre-openbao"
  chmod 600 "$HELIX_DIR/.env.pre-openbao"
  echo "  (backup of the pre-OpenBao .env kept at .env.pre-openbao)"
fi

# 5. Atomic swap (same filesystem)
chmod 600 "$RENDERED"
mv "$RENDERED" "$TARGET"
echo "✓ .env rendered from OpenBao ($(grep -cE '^[A-Z_0-9]+=' "$TARGET") keys)"

# 6. Compose-scoped subset: docker compose auto-reads .env in its own
#    directory, so manual `docker compose` invocations interpolate the real
#    secrets without --env-file. Least-privilege: only the VELARIS_* keys the
#    compose file actually references are copied (never JWT keys etc.).
#    Best-effort — a subset failure must not fail the render.
COMPOSE_DIR="$HELIX_DIR/deploy/docker-compose"
COMPOSE_YML="$COMPOSE_DIR/docker-compose.yml"
if [ -f "$COMPOSE_YML" ]; then
  SUBSET="$COMPOSE_DIR/.env"
  TMP_SUBSET="$SUBSET.tmp.$$"
  {
    echo "# RENDERED by scripts/secrets-render.sh — do not edit; edit via secrets-push.sh"
    grep -oE '\$\{VELARIS_[A-Z_0-9]+' "$COMPOSE_YML" | sed 's/^..//' | sort -u | while read -r key; do
      grep -E "^${key}=" "$TARGET" || echo "# ${key} missing from .env — compose will fail loudly (by design)"
    done
  } > "$TMP_SUBSET" && chmod 600 "$TMP_SUBSET" && mv "$TMP_SUBSET" "$SUBSET" \
    && echo "✓ compose env subset rendered ($SUBSET)" \
    || { rm -f "$TMP_SUBSET"; echo "⚠ compose env subset not written (render itself succeeded)"; }
fi
