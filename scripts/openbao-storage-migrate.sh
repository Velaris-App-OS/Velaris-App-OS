#!/usr/bin/env bash
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
#
# One-time move of OpenBao's storage from "file" to "raft" (November 2026).
#
# OpenBao 2.7 removed the file backend, so an install that still holds file
# storage cannot start the current OpenBao until its data is copied into raft.
# start-velaris.sh and setup-velaris.sh call this BEFORE they start OpenBao.
#
#   ./scripts/openbao-storage-migrate.sh
#
# What it decides, every time (idempotent):
#   - no file storage on this host       → nothing to do (new install, or done)
#   - raft already migrated (marker)     → nothing to do
#   - file storage, raft not complete    → stop OpenBao, discard any partial raft
#                                          data, copy file → raft, write marker
#
# The copy is `bao operator migrate`: it moves the ENCRYPTED data as-is, needs
# no unseal and no token, and the same unseal keys open the vault afterwards.
# It runs with the last OpenBao that can read file storage (2.5.4) as the
# openbao user, so the server can write what it produced.
# The source is mounted read-write only because `migrate` writes its own lock
# file there (core/_migration.temp); it changes no data.
#
# The file data is NEVER deleted: the `openbao-data` volume stays as it was,
# as the way back (pin openbao 2.5.4 and the previous server.hcl). Removing it
# is the operator's act, after the migrated vault has been seen working.
#
# Exit 0 = nothing to do or migrated; non-zero = migration failed (OpenBao then
# starts uninitialised and the platform runs on the existing .env, loudly).

set -euo pipefail
VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
MIGRATE_IMAGE="openbao/openbao:2.5.4"   # last release with the file backend
MARKER=".migrated-from-file-storage"

_env_args=()
[ -f "$VELARIS_DIR/.env" ] && _env_args=(--env-file "$VELARIS_DIR/.env")

# The compose project's own volume names (the project prefix differs per install).
project="$(docker compose -f "$COMPOSE_FILE" "${_env_args[@]}" config --format json 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' 2>/dev/null || true)"
[ -n "$project" ] || project="docker-compose"
data_vol="${project}_openbao-data"
raft_vol="${project}_openbao-file"

_in_vol() {  # run a shell command against both volumes as the openbao user
  docker run --rm --user openbao --entrypoint /bin/sh \
    -v "$data_vol:/openbao/data" -v "$raft_vol:/openbao/file" \
    "$MIGRATE_IMAGE" -c "$1"
}

docker volume inspect "$data_vol" >/dev/null 2>&1 || exit 0     # never had file storage
docker volume inspect "$raft_vol" >/dev/null 2>&1 || docker volume create \
  --label "com.docker.compose.project=$project" --label "com.docker.compose.volume=openbao-file" \
  "$raft_vol" >/dev/null

state="$(_in_vol '
  if [ -f /openbao/file/'"$MARKER"' ]; then echo done
  elif [ -d /openbao/data/core ]; then echo migrate
  else echo none; fi' 2>/dev/null || echo error)"

case "$state" in
  done|none) exit 0 ;;
  migrate) ;;
  *) echo "  ✗ OpenBao storage check failed (could not read the volumes)" >&2; exit 1 ;;
esac

echo "  ▶ Moving OpenBao storage from file to raft (one time; the file data is kept)..."
# The server must not run while its storage is copied.
docker compose -f "$COMPOSE_FILE" "${_env_args[@]}" stop openbao >/dev/null 2>&1 || true
docker rm -f velaris-openbao >/dev/null 2>&1 || true

# A previous attempt that died half-way leaves partial raft data with no
# marker. Start clean rather than migrate on top of it.
_in_vol 'rm -rf /openbao/file/raft /openbao/file/vault.db' >/dev/null 2>&1 || true

if docker run --rm --user openbao --entrypoint /bin/sh \
     -v "$data_vol:/openbao/data" -v "$raft_vol:/openbao/file" \
     "$MIGRATE_IMAGE" -c '
       cat > /tmp/migrate.hcl <<EOF
storage_source "file" {
  path = "/openbao/data"
}
storage_destination "raft" {
  path    = "/openbao/file"
  node_id = "velaris-1"
}
cluster_addr = "http://127.0.0.1:8201"
EOF
       bao operator migrate -config=/tmp/migrate.hcl -log-level=warn \
         && touch /openbao/file/'"$MARKER"
then
  echo "  ✓ OpenBao storage moved to raft — same unseal keys; file data kept in volume $data_vol"
  exit 0
fi

echo "  ✗ OpenBao storage migration FAILED. The file data is untouched in $data_vol." >&2
echo "    OpenBao will start empty; the platform keeps running on the existing .env." >&2
echo "    Re-run: ./scripts/openbao-storage-migrate.sh" >&2
exit 1
