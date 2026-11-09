#!/usr/bin/env bash
# Group K — render .env from OpenBao (one-shot agent, AppRole auth).
#
# Fail-closed by design: if OpenBao is sealed, unreachable, or the render
# comes back empty/implausible, the existing .env is left untouched and we
# exit non-zero. The platform then starts on the last-known-good secrets.
#
#   ./scripts/secrets-render.sh           # render to .env
#   ./scripts/secrets-render.sh --check   # render to stdout key names only (no write)
#   ./scripts/secrets-render.sh --allow-drop  # permit a render that removes keys .env has
set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$VELARIS_DIR/deploy/openbao/agent"
OUT_DIR="$AGENT_DIR/out"
RENDERED="$OUT_DIR/.env"
TARGET="$VELARIS_DIR/.env"

mkdir -p "$OUT_DIR"
rm -f "$RENDERED"

# 1. Server must be up AND unsealed (health 200/429 = active/standby unsealed)
HEALTH=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8350/v1/sys/health" || true)
case "$HEALTH" in
  200|429) ;;
  503) echo "✗ OpenBao is SEALED — unseal first. .env left untouched."; exit 1 ;;
  501) echo "✗ OpenBao not initialized — run ./scripts/secrets-init.sh. .env left untouched."; exit 1 ;;
  *)   echo "✗ OpenBao unreachable on :8350 (health=$HEALTH). .env left untouched."; exit 1 ;;
esac

# 2. One-shot agent render — throwaway container in the server's netns,
#    running as the invoking user so the bind-mounted output is writable.
#    The agent retries a refused or missing AppRole sign-in forever, which hung
#    start and `restore --verify` (R5b re-run 2026-10-30): check the files, and
#    bound the run.
for f in role_id secret_id; do
  [ -s "$AGENT_DIR/$f" ] || { echo "✗ OpenBao agent sign-in file missing: deploy/openbao/agent/$f — run ./scripts/secrets-init.sh. .env left untouched."; exit 1; }
done
RENDER_NAME="velaris-secrets-render-$$"
RENDER_TIMEOUT="${VELARIS_SECRETS_RENDER_TIMEOUT:-60}"
if ! timeout "$RENDER_TIMEOUT" docker run --rm --name "$RENDER_NAME" \
  --network "container:velaris-openbao" \
  --user "$(id -u):$(id -g)" \
  -v "$AGENT_DIR:/openbao/agent" \
  openbao/openbao:2.7.1 bao agent -config=/openbao/agent/agent.hcl >/dev/null; then
  docker rm -f "$RENDER_NAME" >/dev/null 2>&1 || true
  echo "✗ OpenBao agent did not render within ${RENDER_TIMEOUT}s (sign-in refused? check deploy/openbao/agent/role_id and secret_id) — .env left untouched"
  exit 1
fi

# 3. Sanity gates before touching .env
[ -s "$RENDERED" ] || { echo "✗ render produced no output — .env left untouched"; exit 1; }
grep -q '^VELARIS_CASE_DATABASE_URL=' "$RENDERED" \
  || { echo "✗ rendered file missing anchor key VELARIS_CASE_DATABASE_URL — .env left untouched"; exit 1; }

# 3b. Nothing that exists TODAY may silently vanish tomorrow.
#
# An operator who turns a feature on by adding its key to .env by hand, and
# never pushes it to OpenBao, loses it at the next render — which on a reboot
# means the platform quietly comes back with that feature OFF and nothing
# saying so. That is exactly how workload identity (#18) switched itself off,
# and it would do the same to any future secret. Refuse the swap and name the
# keys; fail-closed matches the rest of this script.
if [ -f "$TARGET" ] && [ "${1:-}" != "--allow-drop" ]; then
  _keys() { sed -nE 's/^([A-Z_0-9]+)=.*/\1/p' "$1" | sort -u; }
  DROPPED=$(comm -23 <(_keys "$TARGET") <(_keys "$RENDERED") || true)
  if [ -n "$DROPPED" ]; then
    echo "✗ the render would DROP keys that .env has today — .env left untouched:"
    printf '    %s\n' $DROPPED
    echo "  Push them first:  ./scripts/secrets-push.sh KEY=VALUE"
    echo "  Or, if the removal is deliberate:  $0 --allow-drop"
    rm -f "$RENDERED"
    exit 1
  fi
fi

if [ "${1:-}" = "--check" ]; then
  echo "✓ render OK — keys:"
  grep -oE '^[A-Z_0-9]+' "$RENDERED" | sed 's/^/    /'
  rm -f "$RENDERED"
  exit 0
fi

# 4. First render keeps a one-time backup of the hand-maintained file
if [ -f "$TARGET" ] && [ ! -f "$VELARIS_DIR/.env.pre-openbao" ]; then
  cp -p "$TARGET" "$VELARIS_DIR/.env.pre-openbao"
  chmod 600 "$VELARIS_DIR/.env.pre-openbao"
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
COMPOSE_DIR="$VELARIS_DIR/deploy/docker-compose"
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
