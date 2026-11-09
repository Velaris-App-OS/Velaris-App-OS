#!/usr/bin/env bash
# Internal certificate authority for service-to-service TLS (roadmap #18).
#
#   deploy/tls/make-internal-ca.sh [output-dir]
#
# Creates a private CA and one certificate per service, plus a client
# certificate for the gateway. Nothing here is enabled by doing this: the
# services read `VELARIS_INTERNAL_TLS_ENABLED`, which is off by default, and the
# gateway's proxy_ssl block is empty until the same switch is thrown. Generating
# certificates and turning them on are deliberately two decisions.
#
# **Why this exists when everything is on loopback.** It does not protect
# today's traffic — nothing can reach it. It exists so that moving one service
# to another host is a configuration change rather than a design exercise, and
# so the failure mode of that move is "connection refused", not "plaintext API
# on a network".
#
# The CA key never leaves this directory and never goes in the repository:
# deploy/tls/certs/ is gitignored. Losing it means re-running this script and
# restarting everything, which is a minute of downtime, not a disaster — these
# certificates authenticate services to each other, they encrypt nothing at rest
# and they sign no data.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

OUT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/certs}"
CA_DAYS=3650      # the root is long-lived; rotating it is a coordinated event
LEAF_DAYS=90      # leaves are short by design — see rotate-internal-certs.sh

# Every service that terminates TLS, and the CN it presents. `localhost` is in
# every SAN list because that is genuinely how the gateway reaches them today;
# the service name is there for the day they move to separate hosts.
SERVICES=(case-service analytics-service hxgraph-service hxmigrate-service worker-service)
# Services that ALSO call another service directly (R5e steps 7, 8, 10: commerce-,
# portal- and user-service call case-service's /internal API). Their certificate carries
# both server and client use; every other service stays server-only, so no other
# service can present itself as a caller.
CALLERS=(commerce-service portal-service user-service)

mkdir -p "$OUT_DIR"
# Docker creates this directory as root when the gateway starts before it exists
# (older installs). Say so and how to fix it, rather than "chmod: Operation not
# permitted" halfway through.
if [ ! -w "$OUT_DIR" ]; then
  echo "✗ $OUT_DIR is not writable by $(id -un) (owner: $(stat -c %U "$OUT_DIR" 2>/dev/null || echo '?'))." >&2
  echo "  Docker created it when the gateway first started. Give it back, then re-run:" >&2
  echo "    sudo chown $(id -un): \"$OUT_DIR\"" >&2
  exit 1
fi
chmod 700 "$OUT_DIR"
cd "$OUT_DIR"

if [ -f ca.key ]; then
  echo "A CA already exists in $OUT_DIR."
  echo "Refusing to overwrite it — every issued certificate would stop verifying"
  echo "at once. To start over, move the directory aside first."
  exit 1
fi

echo "▶ Creating the internal CA (valid ${CA_DAYS} days)"
openssl genrsa -out ca.key 4096 2>/dev/null
chmod 600 ca.key
openssl req -x509 -new -nodes -key ca.key -sha256 -days "$CA_DAYS" -out ca.crt \
  -subj "/O=Velaris/OU=Internal/CN=Velaris Internal CA" 2>/dev/null

_issue() {
  local name="$1" extra_san="$2" is_client="$3"
  echo "▶ Issuing ${name}"
  openssl genrsa -out "${name}.key" 2048 2>/dev/null
  chmod 600 "${name}.key"
  openssl req -new -key "${name}.key" -out "${name}.csr" \
    -subj "/O=Velaris/OU=Internal/CN=${name}" 2>/dev/null

  local ext_usage="serverAuth"
  [ "$is_client" = "client" ] && ext_usage="clientAuth"
  [ "$is_client" = "both" ] && ext_usage="serverAuth, clientAuth"

  cat > "${name}.ext" <<EXT
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = ${ext_usage}
subjectAltName = DNS:${name},DNS:localhost,IP:127.0.0.1${extra_san}
EXT

  openssl x509 -req -in "${name}.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out "${name}.crt" -days "$LEAF_DAYS" -sha256 -extfile "${name}.ext" 2>/dev/null
  rm -f "${name}.csr" "${name}.ext"
}

for svc in "${SERVICES[@]}"; do
  _issue "$svc" "" server
done
for svc in "${CALLERS[@]}"; do
  _issue "$svc" "" both
done

# The gateway is the CLIENT in every internal hop: it is what the services must
# authenticate. It presents this certificate; they verify it against ca.crt.
_issue "gateway" "" client
# The website's registration service calls user-service's /internal/v1/demo
# routes directly (2026-11-04). Client-only: it serves nothing to the platform.
_issue "website" "" client

chmod 644 ca.crt ./*.crt

echo
echo "✓ Certificates written to $OUT_DIR"
echo "  CA valid ${CA_DAYS} days · service certificates valid ${LEAF_DAYS} days"
echo
echo "Nothing is switched on yet. To enable internal TLS:"
echo "  1. Set VELARIS_INTERNAL_TLS_ENABLED=true in .env"
echo "  2. Restart the services and recreate the gateway container"
echo "  3. Install the rotation timer:  sudo deploy/tls/install-rotation-timer.sh"
echo
echo "Leaf certificates expire in ${LEAF_DAYS} days. That is short ON PURPOSE:"
echo "a rotation path exercised four times a year works when you need it, and one"
echo "exercised every two years does not. Install the timer."
