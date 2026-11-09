#!/usr/bin/env bash
# Velaris — build the release tarball an update server serves.
#
#   scripts/package-release.sh [--version X.Y.Z] [--out DIR]
#
# Writes DIR/vX.Y.Z.tar.gz (default DIR = ./dist-release), the file
# update-velaris.sh downloads from `<server>/download/vX.Y.Z.tar.gz` when
# velaris.yaml says `updates.source: server`. One top-level folder (`velaris/`),
# because the updater unpacks with --strip-components=1.
#
# What goes in: the release tree, minus everything the mirror exclude list names
# (local build/runtime state and anything an install generates — keys, certs,
# the install report). Read scripts/velaris-mirror-exclude.txt: the same rule
# keeps a box's identity out of the release twin and out of a package.
#
# What it adds: `.release-files`, the list of every file in this release. An
# install keeps it, and the next tarball update removes exactly the files this
# release shipped and the next one does not (R3 2026-11-01: a rollback left the
# newer release's services behind and stopped at `uv sync`). Without the list
# an update removes nothing, as before.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION=""
OUT="$ROOT/dist-release"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:-}"; shift 2 ;;
    --out)     OUT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$VERSION" ]] || VERSION=$(grep -m1 'version:' "$ROOT/velaris.yaml" | sed 's/.*version: *"\([^"]*\)".*/\1/')
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "✗ not a release version: '$VERSION'" >&2; exit 1; }

# What a release carries: the release twin's top level, which is the list a
# customer install has. Anything at the top of this repo that the twin does not
# have (notes, marketing, the website, test benches) is not product.
TWIN="$ROOT/Velaris"
[[ -d "$TWIN" ]] || { echo "✗ $TWIN not found — it names what a release contains" >&2; exit 1; }
mapfile -t ITEMS < <(cd "$TWIN" && ls -1A | grep -vxE 'fonts|git_push\.sh|\.env|\.git|\.gitignore')
for extra in restore-velaris.sh standby-velaris.sh support-bundle.sh SECURITY.md CHANGELOG.md; do
  [[ -e "$ROOT/$extra" ]] && ITEMS+=("$extra")
done

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/velaris-package.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/velaris"
present=()
for i in "${ITEMS[@]}"; do
  if [[ -e "$ROOT/$i" ]]; then present+=("$i")
  # A file only the twin holds (LICENSE-APACHE) comes from the twin.
  elif [[ -f "$TWIN/$i" ]]; then cp "$TWIN/$i" "$STAGE/velaris/$i"; fi
done
(cd "$ROOT" && rsync -a --exclude-from=scripts/velaris-mirror-exclude.txt "${present[@]}" "$STAGE/velaris/")

# The version this package installs, whatever the working tree says.
sed -i "0,/^  version: \"[^\"]*\"/s//  version: \"$VERSION\"/" "$STAGE/velaris/velaris.yaml"

# Refuse to package a box's identity. The exclude list should already have
# dropped these; this is the second look, not the first.
leaks=$(cd "$STAGE/velaris" && find . \( -name '*.key' -o -name 'role_id' -o -name 'secret_id' \
          -o -name '.bao-init.json' -o -name 'velaris-install-report.md' -o -name '.env' \) -print)
[[ -z "$leaks" ]] || { echo "✗ refusing: install-generated secrets in the package:" >&2; echo "$leaks" >&2; exit 1; }

(cd "$STAGE/velaris" && find . -type f -o -type l | sed 's|^\./||' | grep -vx '.release-files' | LC_ALL=C sort > .release-files)
echo ".release-files" >> "$STAGE/velaris/.release-files"

mkdir -p "$OUT"
# Files and links only — NO folder entries. An updater that runs as root (every
# one up to 3.1.17) sets an existing folder's owner and mode from the archive,
# so a folder entry carried THIS machine's state onto every install: on
# 2026-11-06 the packager's own 0700 pooler-socket folder reached vx-load and
# stopped the platform. Without the entry tar leaves an existing folder alone
# and makes a missing one. File owners are left as they were (an older updater
# applies them as before); the updater from 3.1.18 gives the files to the
# install's owner instead.
(cd "$STAGE" && sed 's|^|velaris/|' velaris/.release-files \
   | tar -czf "$OUT/v$VERSION.tar.gz" --no-recursion --verbatim-files-from -T -)
echo "✓ $OUT/v$VERSION.tar.gz ($(wc -l < "$STAGE/velaris/.release-files") files, $(du -h "$OUT/v$VERSION.tar.gz" | cut -f1))"
