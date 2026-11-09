#!/usr/bin/env bash
# Velaris — take a backup set AND record it in HxDR, in one command.
#
# What a backup is (since November 2026)
# --------------------------------------
# ONE encrypted file per run, `velaris-<host>-<UTC>-v<version>.tar.age`, holding
# the database (consistent dump + exact row counts), the documents, the .env
# (with the keys that make restored documents readable), the OpenBao vault
# snapshot and a manifest of checksums. Encrypted to this install's age public
# key (VELARIS_BACKUP_AGE_RECIPIENT); the private key is not kept on the server
# once you have run --forget-private-key. Copied to VELARIS_BACKUP_PATH (a
# mounted NAS/disk) and/or VELARIS_BACKUP_S3_* when set, and pruned to
# 7 daily / 4 weekly / 6 monthly. Every run is recorded in HxDR, so
# `GET /dr/posture` reports what really happened.
#
# Without a key it does what it always did: a database-only dump, local, plain
# SQL — and says so. Settings and the vault are never written unencrypted.
#
# Design: docs/Future/Done/backup-restore-design.md. Engine:
# scripts/backup/velaris_backup.py.
#
# Usage
# -----
#   ./backup-velaris.sh                       # take a set + record it
#   ./backup-velaris.sh --dir /mnt/backups    # local copy somewhere other than .backups/
#   ./backup-velaris.sh --quiet               # timer/cron friendly: errors only
#   ./backup-velaris.sh --keep 14             # keyless dumps only: delete those older than N days
#   ./backup-velaris.sh --test-restore        # prove THIS set restores (otherwise weekly,
#                                             # VELARIS_BACKUP_TEST_EVERY_DAYS) — the dump is
#                                             # loaded into a throwaway database and checked
#   ./backup-velaris.sh --ensure-key          # give this install a key pair if it has none
#   ./backup-velaris.sh --forget-private-key  # after copying it off: remove the private
#   ./backup-velaris.sh --push-deletions     # copy purged accounts' signed records off the box
#                                            # (also every backup run, and at once by a path unit)
#                                             # key from velaris-install-report.md
#
# Exit status
# -----------
#   0 complete set (and every configured off-box copy made)
#   1 failed — nothing usable was produced
#   3 PARTIAL set — taken, but a part is missing (named in the output)
#   4 set taken, but no off-box copy succeeded
#   5 set taken, but it FAILED its restore test
# A failure to RECORD in HxDR never changes the exit code.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$VELARIS_DIR/.venv/bin/python"
ENGINE="$VELARIS_DIR/scripts/backup/velaris_backup.py"

if [[ ! -x "$PY" ]]; then
  echo "✗ $PY not found — run ./setup-velaris.sh first" >&2
  exit 1
fi

CMD=(backup)
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)                EXTRA+=(--dir "$2"); shift 2 ;;
    --keep)               EXTRA+=(--keep "$2"); shift 2 ;;
    --quiet)              EXTRA+=(--quiet); shift ;;
    --test-restore)       EXTRA+=(--test-restore); shift ;;
    --no-test-restore)    EXTRA+=(--no-test-restore); shift ;;
    --ensure-key)         CMD=(ensure-key); shift ;;
    --forget-private-key) CMD=(forget-private-key); shift ;;
    --push-deletions)     CMD=(push-deletions); shift ;;
    -h|--help)            sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# --dir / --keep belong to `backup` only.
if [[ "${CMD[0]}" != backup ]]; then
  _keep=()
  for a in "${EXTRA[@]+"${EXTRA[@]}"}"; do if [[ "$a" == --quiet ]]; then _keep+=(--quiet); fi; done
  EXTRA=("${_keep[@]+"${_keep[@]}"}")
fi

cd "$VELARIS_DIR"
exec "$PY" "$ENGINE" "${CMD[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
