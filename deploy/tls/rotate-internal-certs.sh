#!/usr/bin/env bash
# Renew the internal service certificates before they expire (roadmap #18).
#
#   deploy/tls/rotate-internal-certs.sh [--force] [--days-before N] [--no-reload]
#
# Run daily by velaris-cert-rotate.timer. Renews any leaf within N days of
# expiry (default 21), leaves the CA alone, then restarts what has to reload to
# pick up new key material.
#
# **The part that actually fails.** Not the issuing — the reload. uvicorn reads
# its certificate once at startup and never again, so a rotation that writes new
# files and does not restart the service looks completely successful and then
# everything stops on expiry day. This script therefore treats "restarted" as
# part of rotating, and exits non-zero if it could not.
#
# **And the part nobody notices.** A rotation job that silently stops running
# leaves you with certificates that expire on a schedule you have forgotten.
# `--check` prints days-to-expiry for every leaf and exits 1 if any is inside
# the window; point a monitor at that rather than trusting the timer. This is
# exactly the trap the SAML certificate work hit on 2026-08-17 — the renewal was
# fine, the *watching* was missing.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="${VELARIS_INTERNAL_TLS_DIR:-$SCRIPT_DIR/certs}"
DAYS_BEFORE=21
LEAF_DAYS=90
NO_RELOAD=0
FORCE=0
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --check) CHECK_ONLY=1 ;;
    # Renew and stop there. Used by the launch path, which calls this BEFORE a
    # service starts: there is nothing running to restart, and restarting the
    # other three mid-boot would fight systemd for control of a starting unit.
    --no-reload) NO_RELOAD=1 ;;
    --days-before) DAYS_BEFORE="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -f "$CERT_DIR/ca.crt" ] || { echo "No CA in $CERT_DIR — run make-internal-ca.sh first" >&2; exit 1; }

LEAVES=(case-service analytics-service hxgraph-service hxmigrate-service commerce-service portal-service user-service worker-service gateway website)

_days_left() {
  local end
  end="$(openssl x509 -enddate -noout -in "$1" | cut -d= -f2)"
  echo $(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 ))
}

# ── --check: report, never change anything ───────────────────────
if [ "$CHECK_ONLY" = 1 ]; then
  rc=0
  for name in "${LEAVES[@]}" ca; do
    f="$CERT_DIR/${name}.crt"
    [ "$name" = website ] && [ ! -f "$f" ] && continue   # only where a website was set up
    [ -f "$f" ] || { echo "MISSING  ${name}"; rc=1; continue; }
    d="$(_days_left "$f")"
    if [ "$d" -lt "$DAYS_BEFORE" ]; then
      echo "EXPIRING ${name}: ${d} days left"; rc=1
    else
      echo "ok       ${name}: ${d} days left"
    fi
  done
  exit $rc
fi

RENEWED=0
for name in "${LEAVES[@]}"; do
  f="$CERT_DIR/${name}.crt"
  # The website's client certificate (2026-11-04) is issued by setup-website.sh,
  # on the operator's run — never here, where a missing one would restart every
  # service on an install that has no website.
  if [ "$name" = website ] && [ ! -f "$f" ]; then continue; fi
  # A service added after the CA was made (commerce-service, R5e step 7) has no
  # certificate yet: issue it from the existing CA rather than stop — the CA
  # script refuses to run twice, so this is the only way an install gets one.
  if [ -f "$f" ]; then d="$(_days_left "$f")"; else d=0; echo "▶ ${name}: no certificate yet — issuing one"; fi
  if [ -f "$f" ] && [ "$FORCE" != 1 ] && [ "$d" -ge "$DAYS_BEFORE" ]; then
    echo "  ${name}: ${d} days left, not due"
    continue
  fi

  echo "▶ Renewing ${name} (${d} days left)"
  usage="serverAuth"; case "$name" in gateway|website) usage="clientAuth" ;; esac
  # Call case-service's /internal API directly (R5e steps 7, 8) — server AND client.
  case "$name" in commerce-service|portal-service|user-service) usage="serverAuth, clientAuth" ;; esac
  cat > "$CERT_DIR/${name}.ext" <<EXT
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = ${usage}
subjectAltName = DNS:${name},DNS:localhost,IP:127.0.0.1
EXT
  # A NEW key each time, not just a new certificate: rotation whose key never
  # changes only renews the expiry date and none of the reason for rotating.
  openssl genrsa -out "$CERT_DIR/${name}.key.new" 2048 2>/dev/null
  openssl req -new -key "$CERT_DIR/${name}.key.new" -out "$CERT_DIR/${name}.csr" \
    -subj "/O=Velaris/OU=Internal/CN=${name}" 2>/dev/null
  openssl x509 -req -in "$CERT_DIR/${name}.csr" \
    -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
    -out "$CERT_DIR/${name}.crt.new" -days "$LEAF_DAYS" -sha256 \
    -extfile "$CERT_DIR/${name}.ext" 2>/dev/null
  # Swap only once BOTH halves exist — a crash between the two would otherwise
  # leave a key that does not match its certificate, which fails at handshake
  # time on the next restart rather than here where someone is watching.
  mv "$CERT_DIR/${name}.key.new" "$CERT_DIR/${name}.key"
  mv "$CERT_DIR/${name}.crt.new" "$CERT_DIR/${name}.crt"
  chmod 600 "$CERT_DIR/${name}.key"; chmod 644 "$CERT_DIR/${name}.crt"
  # The timer runs this as root so it can restart the services afterwards, which
  # would leave the new files root-owned — and the services do NOT run as root,
  # so they could not read a 0600 key they had just been handed. Give them back
  # to whoever owns the directory, which is the account that made the CA.
  if [ "$(id -u)" = 0 ]; then
    owner="$(stat -c '%U:%G' "$CERT_DIR")"
    chown "$owner" "$CERT_DIR/${name}.key" "$CERT_DIR/${name}.crt" 2>/dev/null || true
  fi
  rm -f "$CERT_DIR/${name}.csr" "$CERT_DIR/${name}.ext"
  RENEWED=$((RENEWED + 1))
done

# The website's container mounts only certs/website/ — never this directory,
# which holds ca.key. Keep its copy current (2026-11-04).
if [ -f "$CERT_DIR/website.crt" ]; then
  mkdir -p "$CERT_DIR/website" && chmod 700 "$CERT_DIR/website"
  cp -f "$CERT_DIR/ca.crt" "$CERT_DIR/website.crt" "$CERT_DIR/website.key" "$CERT_DIR/website/"
  chmod 600 "$CERT_DIR/website/website.key"
fi

if [ "$RENEWED" = 0 ]; then
  echo "Nothing due."
  exit 0
fi

if [ "$NO_RELOAD" = 1 ]; then
  echo "Renewed ${RENEWED} certificate(s); not reloading (--no-reload)."
  exit 0
fi

# ── The reload, which is the half that matters ───────────────────
echo "▶ Reloading services so the new key material is actually used"
FAILED=""
UNMANAGED=""
# service:port — the port is how we tell "not installed" from "running, but not
# under systemd". `is-active` is false in BOTH cases, so restarting only what
# systemd knows about would skip a live service in silence and then report
# success: the gateway would come back holding new key material while four
# uvicorn processes served the OLD certificate until it expired. That is the
# exact failure this script exists to prevent, and it is the shape of the dev
# box, where the services run under `nohup` rather than systemd.
for pair in velaris-case-service:8201 velaris-analytics-service:8202 \
            velaris-hxgraph-service:8203 velaris-hxmigrate-service:8204 \
            velaris-commerce-service:8207 velaris-portal-service:8205 \
            velaris-user-service:8206 \
            velaris-worker-service:8208; do
  unit="${pair%%:*}"; port="${pair##*:}"
  # Without its own unit, an extracted service runs inside case-service's unit
  # (velaris-exec.sh) and was just restarted with it — not an unmanaged process.
  if [ "$unit" != velaris-case-service ] && [ ! -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/$unit.service" ] \
     && systemctl is-active --quiet velaris-case-service 2>/dev/null; then
    continue
  fi
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    systemctl restart "$unit" || FAILED="$FAILED $unit"
  elif command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${port}[[:space:]]"; then
    UNMANAGED="$UNMANAGED ${unit}(:${port})"
  fi
done
# nginx re-reads its files on reload, but the gateway runs as a container with a
# bind-mounted config, so a reload signal is not enough on every path — restart.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^velaris-gateway$'; then
  docker restart velaris-gateway >/dev/null || FAILED="$FAILED velaris-gateway"
fi

if [ -n "$UNMANAGED" ]; then
  echo "✗ Renewed ${RENEWED} certificate(s), but these are LISTENING and not under" >&2
  echo "  systemd, so nothing here can restart them:${UNMANAGED}" >&2
  echo "  They are still serving the OLD certificate and will keep doing so until" >&2
  echo "  it expires. Restart them now — ./stop-velaris.sh && ./start-velaris.sh —" >&2
  echo "  or install the units so rotation can do it unattended." >&2
  FAILED="$FAILED$UNMANAGED"
fi

if [ -n "$FAILED" ]; then
  echo "✗ Renewed ${RENEWED} certificate(s) but could not restart:${FAILED}" >&2
  echo "  Those processes are still using the OLD key material. Restart them by" >&2
  echo "  hand — the new certificates take effect only at startup." >&2
  exit 1
fi

echo "✓ Renewed ${RENEWED} certificate(s) and restarted everything that needed it"
