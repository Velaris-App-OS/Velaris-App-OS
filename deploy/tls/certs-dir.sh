#!/usr/bin/env bash
# Velaris — make sure deploy/tls/certs exists and belongs to the install.
#
#   . deploy/tls/certs-dir.sh; velaris_tls_certs_dir "$VELARIS_DIR"
#
# The gateway mounts ../tls/certs whether internal TLS is on or not (the mount
# is how it is turned on). When the directory does not exist at the first
# `docker compose up`, Docker creates it — owned by root. Then the documented
# way to turn internal TLS on, deploy/tls/make-internal-ca.sh run as the
# install's user, cannot write into it and the gateway will not start
# (R3 2026-11-01). So: create it first, as the install's user, before any
# `compose up`; and give back one Docker already created, if it is still empty
# and a non-interactive sudo is allowed. Never touches a directory with files in
# it, never prompts, never fails the caller.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

velaris_tls_certs_dir() {  # VELARIS_DIR
  local dir="${1:-.}/deploy/tls/certs"
  if [ ! -e "$dir" ]; then
    mkdir -p "$dir" 2>/dev/null && chmod 700 "$dir" 2>/dev/null
    return 0
  fi
  [ -d "$dir" ] && [ ! -w "$dir" ] || return 0
  [ -z "$(ls -A "$dir" 2>/dev/null)" ] || return 0
  if sudo -n chown "$(id -u):$(id -g)" "$dir" 2>/dev/null; then
    chmod 700 "$dir" 2>/dev/null
    echo "  ✓ deploy/tls/certs given back to $(id -un) (Docker had created it as root)"
  fi
  return 0
}
