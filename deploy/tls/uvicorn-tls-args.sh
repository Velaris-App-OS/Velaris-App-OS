# Shared by start-velaris.sh and the systemd exec wrapper (roadmap #18).
# Source it, then:  _velaris_tls_args case-service   → echoes the uvicorn flags.
#
# Prints nothing at all when internal TLS is off, which is the default — so both
# launch paths keep exactly the command line they had, and there is one place
# that decides rather than two that can disagree.
#
# `--ssl-cert-reqs 2` is ssl.CERT_REQUIRED: the service demands a client
# certificate and verifies it against the internal CA. Without it the service
# would encrypt the hop and accept a connection from anyone — the direction of
# authentication that actually matters here is the gateway proving itself to the
# service, not the other way round.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

_velaris_tls_args() {
  local service="$1"
  [ "${VELARIS_INTERNAL_TLS_ENABLED:-false}" = "true" ] || return 0

  local dir="${VELARIS_INTERNAL_TLS_DIR:-deploy/tls/certs}"
  case "$dir" in /*) ;; *) dir="${VELARIS_DIR:-$PWD}/$dir" ;; esac

  # ── Self-heal an expired certificate, before the service starts ────────
  #
  # A machine that was off across an expiry comes back with leaves that lapsed
  # while it slept. The rotation timer does replace them — a negative
  # days-to-expiry is still "inside the window" — but only when it next fires,
  # which is up to its 30-minute jitter after boot, and until then every
  # handshake fails and the platform is down.
  #
  # Renewing HERE closes that gap and is the easiest place to do it: nothing is
  # running yet, so there is no restart to perform and no privilege needed for
  # one. The timer keeps its job — renewing *before* expiry, while everything is
  # up — and this only ever catches the case it cannot: already lapsed at boot.
  #
  # **Expired only, never merely due.** Renewing at 20 days would mint a new key
  # on every ordinary restart during the window, which is churn with no benefit.
  if [ -f "$dir/$service.crt" ] && ! openssl x509 -checkend 0 -noout \
       -in "$dir/$service.crt" >/dev/null 2>&1; then
    local rotate="${VELARIS_DIR:-$PWD}/deploy/tls/rotate-internal-certs.sh"
    if [ -x "$rotate" ]; then
      echo "NOTICE: $service's certificate has expired. Renewing before start." >&2
      # Under a lock: systemd starts four services at once and they would all
      # see the same expired certificates and all run this together, racing on
      # the shared CA serial file and on each other's key/cert pairs. The first
      # one renews every leaf; the other three find them fresh and skip.
      if command -v flock >/dev/null 2>&1; then
        flock "$dir" "$rotate" --no-reload >&2 || \
          echo "WARNING: renewal failed; starting $service as-is." >&2
      else
        "$rotate" --no-reload >&2 || \
          echo "WARNING: renewal failed; starting $service as-is." >&2
      fi
    fi
  fi

  # ── Issue a missing certificate from the install's existing CA ─────────
  # A service added by an update (commerce-service R5e step 7, portal-service
  # step 8, user-service step 10) has no certificate on an install whose CA predates it, and the CA
  # script refuses to run twice. The rotation script issues any known leaf that
  # is missing; doing it here means the new service starts with TLS instead of
  # in plaintext until the timer next fires. Same lock as above.
  if { [ ! -f "$dir/$service.crt" ] || [ ! -f "$dir/$service.key" ]; } \
     && [ -f "$dir/ca.crt" ] && [ -f "$dir/ca.key" ]; then
    local rotate="${VELARIS_DIR:-$PWD}/deploy/tls/rotate-internal-certs.sh"
    if [ -x "$rotate" ]; then
      echo "NOTICE: $service has no certificate yet. Issuing one from the install's CA." >&2
      if command -v flock >/dev/null 2>&1; then
        flock "$dir" "$rotate" --no-reload >&2 || \
          echo "WARNING: issuing failed; see below." >&2
      else
        "$rotate" --no-reload >&2 || echo "WARNING: issuing failed; see below." >&2
      fi
    fi
  fi
  if [ ! -f "$dir/$service.crt" ] || [ ! -f "$dir/$service.key" ]; then
    # Loud, and NOT fatal: a service that refuses to start because a certificate
    # is missing takes the platform down over a setting nobody meant to enable.
    # It starts in plaintext and says so; the gateway, which speaks https when
    # the same flag is set, then fails to connect — visibly, in one place.
    echo "WARNING: VELARIS_INTERNAL_TLS_ENABLED=true but $dir/$service.crt is missing." >&2
    echo "         Starting $service WITHOUT TLS. Run deploy/tls/make-internal-ca.sh." >&2
    return 0
  fi

  printf '%s' "--ssl-keyfile $dir/$service.key --ssl-certfile $dir/$service.crt --ssl-ca-certs $dir/ca.crt --ssl-cert-reqs 2"
}

# ── Health probes ─────────────────────────────────────────────────────────────
#
# The gateway is not the only thing that opens a service port: `start-velaris.sh`
# waits on /health after each launch, and `update-velaris.sh` gates the whole
# update on the same four endpoints. Those probes speak plain http, and with
# `--ssl-cert-reqs 2` a service refuses anything that does not present the
# gateway's CLIENT certificate — at the handshake, before /health is ever
# reached. Left alone, turning mTLS on would make a perfectly healthy platform
# report every service dead, and would make update-velaris.sh call a good update
# a failed one and act on it.
#
# So the same file that decides how a service LISTENS also decides how we KNOCK.
# Prints a plain http probe when TLS is off, so both scripts keep the behaviour
# they had.
#
#   _velaris_probe 8201  → curl … http(s)://localhost:8201/health
#
# `localhost` and not 127.0.0.1 because that is the name in every service's SAN
# list; the hostname check stays ON rather than being disabled to make it work.
_velaris_probe() {
  local port="$1"
  local timeout="${VELARIS_PROBE_TIMEOUT:-3}"

  if [ "${VELARIS_INTERNAL_TLS_ENABLED:-false}" != "true" ]; then
    curl -sf --max-time "$timeout" "http://localhost:${port}/health" >/dev/null 2>&1
    return $?
  fi

  local dir="${VELARIS_INTERNAL_TLS_DIR:-deploy/tls/certs}"
  case "$dir" in /*) ;; *) dir="${VELARIS_DIR:-$PWD}/$dir" ;; esac

  # Same fallback as the launch flags: if the certificates are not there the
  # service started in plaintext and said so, so probe it the way it actually
  # came up rather than reporting it dead for a second reason.
  if [ ! -f "$dir/gateway.crt" ] || [ ! -f "$dir/ca.crt" ]; then
    curl -sf --max-time "$timeout" "http://localhost:${port}/health" >/dev/null 2>&1
    return $?
  fi

  curl -sf --max-time "$timeout" \
    --cacert "$dir/ca.crt" --cert "$dir/gateway.crt" --key "$dir/gateway.key" \
    "https://localhost:${port}/health" >/dev/null 2>&1
}
