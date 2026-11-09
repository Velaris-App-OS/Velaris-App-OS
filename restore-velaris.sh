#!/usr/bin/env bash
# Velaris — restore an install from a backup set, then prove it.
#
#   ./restore-velaris.sh <set.tar.age | s3://bucket/key> --key <private key file> [--start]
#   ./restore-velaris.sh <set> --key <file> --replace-existing [--start]
#   ./restore-velaris.sh --verify
#
# What it does, in order (docs/Future/Done/backup-restore-design.md §6–7):
#   1. decrypts the set with YOUR private key and checks every file's SHA-256
#   2. refuses: a set from a newer Velaris than this code, a different database
#      family, a running platform (stop it first), or an install that is already
#      here — unless --replace-existing, which takes a safety backup first
#   3. restores the OpenBao vault (opened with the set's own unseal key), the
#      .env (what it replaces is kept in .backups/before-restore-<time>/), the database,
#      Temporal's in-flight workflows, and the documents
#   4. checks the row counts against the manifest BEFORE anything starts; with
#      --start (or `--verify` after you start it under systemd): every hash chain
#      is as it was at backup time, the documents that opened then open now, a
#      signed token is accepted, the vault renders .env
#
# Typical use, total loss of a server:
#   on a fresh box:  ./setup-velaris.sh  (same version as the set, or newer)
#                    ./restore-velaris.sh velaris-…tar.age --key ~/backup-key.txt --start
#
# The key file is the private key you copied out of velaris-install-report.md.
# Keep it off the server afterwards.
#
# Exit status: 0 restored (and verified, with --start); 1 refused or failed.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# R5c: a server linked as a STANDBY runs only its two replica databases
# (standby-velaris.sh). Starting the platform here would start a second
# PostgreSQL on the replicas' own data. standby.json exists only while linked.
if [ -f "$VELARIS_DIR/.standby/standby.json" ]; then
  echo "✗ This server is linked as a Velaris standby ($VELARIS_DIR/.standby/standby.json)." >&2
  echo "  The platform must not run here while it is one. To use it on its own, unlink it" >&2
  echo "  first: ./standby-velaris.sh unlink   (on the main server)" >&2
  exit 1
fi
# An old main whose standby was promoted: running it again means two servers
# taking writes. demoted.json stays until it is linked as the new main's standby.
if [ -f "$VELARIS_DIR/.standby/demoted.json" ]; then
  echo "✗ This server was replaced by its standby ($VELARIS_DIR/.standby/demoted.json)." >&2
  echo "  Starting it would make two main servers. To bring it back as a standby:" >&2
  echo "  ./standby-velaris.sh retire   then link it from the new main." >&2
  exit 1
fi
# Step 12 (automatic failover): this main stopped itself after losing both the
# witness and its standby — the standby may have taken over. Its agent lifts
# this by itself once the witness confirms this server still holds the lease.
if [ -f "$VELARIS_DIR/.standby/fenced.json" ]; then
  echo "✗ This server stopped itself: it lost both the failover witness and its standby" >&2
  echo "  ($VELARIS_DIR/.standby/fenced.json). The standby may have taken over." >&2
  echo "  Check: ./standby-velaris.sh failover status   (it restarts by itself if it still holds the lease)" >&2
  exit 1
fi
PY="$VELARIS_DIR/.venv/bin/python"
ENGINE="$VELARIS_DIR/scripts/backup/velaris_backup.py"
[[ -x "$PY" ]] || { echo "✗ $PY not found — run ./setup-velaris.sh first" >&2; exit 1; }

case "${1:-}" in
  ""|-h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
  --verify) shift; cd "$VELARIS_DIR"; exec "$PY" "$ENGINE" verify "$@" ;;
esac

cd "$VELARIS_DIR"
exec "$PY" "$ENGINE" restore "$@"
